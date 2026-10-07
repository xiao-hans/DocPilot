"""已批准的链路阈值判定（`docs/19 §1.2`，负责人 2026-10-07 批准默认值）。

判定必须能失败：两条时延阈值是从观测数据里算出来的数字，超了就在报告里写"不满足"，
观测脚本的退出码跟着变红。处理阈值（真实 OCR/LLM 的单任务耗时与吞吐上限）仍未设定，
这里不判——mock 执行时间没有可判的分子。
"""

from __future__ import annotations

# 租户内列表/检索接口 P95，适用于"≤10 万文档量级"这一档。
LIST_P95_MS = 200.0
# 调度延迟：发布确认 → 开始执行的 P95。
SCHEDULE_P95_MS = 2000.0
# 流式读的进程内存增量上限：阈值本身写的是"与对象大小无关"，这条是它的可测形式。
STREAM_RSS_DELTA_MB = 1.0

# 参与列表/检索阈值的查询名（phase_reads 里逐条计时的键）。
LIST_QUERY_NAMES = (
    "list_jobs",
    "list_documents",
    "list_documents_deep_page",
    "review_queue",
    "search_hit",
    "search_miss",
)

_COVERAGE_GAP = "本轮只覆盖到 {docs} 份文档，10 万级未复测；收紧阈值前需要一次 10 万级观测"


def render_section(payload: dict) -> list[str]:
    """报告段：阈值表 + 汇总行。判定逻辑与渲染放在一起，用例才能直接喂数据证明它会报"不满足"。"""
    verdict = evaluate(payload)
    lines = [
        "## D. 链路阈值判定（`docs/19 §1.2`，2026-10-07 已批）",
        "",
        "| 阈值 | 建议值 | 实测 | 判定 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for row in verdict["rows"]:
        lines.append(f"| {row['阈值']} | {row['建议值']} | {row['实测']} | {row['判定']} | {row['备注']} |")
    if verdict["breaches"]:
        summary = f"**不满足 {len(verdict['breaches'])} 项**：" + "；".join(verdict["breaches"])
    else:
        summary = "已批的三条链路阈值全部满足；处理阈值仍未设定，不参与判定。"
    lines += ["", f"{summary} 不满足项会让脚本退出码为 1。", ""]
    return lines


def evaluate(payload: dict) -> dict:
    """返回 {"rows": [报告行], "breaches": [不满足项]}。"""
    reads = payload.get("reads") or {}
    queue = payload.get("queue") or {}
    large = payload.get("large") or {}
    counts = payload.get("counts") or {}
    latencies = reads.get("latency_ms") or {}

    rows: list[dict] = []
    breaches: list[str] = []

    measured = {name: latencies[name] for name in LIST_QUERY_NAMES if name in latencies}
    docs = counts.get("documents", 0)
    if measured:
        worst_name, worst_entry = max(measured.items(), key=lambda item: item[1]["p95"])
        worst = worst_entry["p95"]
        ok = worst <= LIST_P95_MS
        rows.append(
            {
                "阈值": "租户内列表/检索接口 P95（≤10 万文档量级）",
                "建议值": f"≤ {LIST_P95_MS:.0f} ms",
                "实测": f"{worst} ms（最差 `{worst_name}`，{docs} 份文档）",
                "判定": "满足" if ok else "不满足",
                "备注": _COVERAGE_GAP.format(docs=docs) if ok else "数值超出已批阈值",
            }
        )
        if not ok:
            breaches.append(f"列表/检索 P95 {worst} ms 超过 {LIST_P95_MS:.0f} ms")
    else:
        rows.append(
            {
                "阈值": "租户内列表/检索接口 P95（≤10 万文档量级）",
                "建议值": f"≤ {LIST_P95_MS:.0f} ms",
                "实测": "无数据",
                "判定": "未判定",
                "备注": "读路径阶段没有产出任何计时样本，判不了",
            }
        )

    schedule = (queue.get("schedule_delay_ms") or {}).get("p95")
    if schedule is not None:
        ok = schedule <= SCHEDULE_P95_MS
        rows.append(
            {
                "阈值": "调度延迟 P95（发布确认 → 开始执行）",
                "建议值": f"≤ {SCHEDULE_P95_MS:.0f} ms",
                "实测": f"{schedule} ms（同机 {queue.get('workers')} 个 Worker）",
                "判定": "满足" if ok else "不满足",
                "备注": "不含 Worker 冷启动；扩容成本另计" if ok else "数值超出已批阈值",
            }
        )
        if not ok:
            breaches.append(f"调度延迟 P95 {schedule} ms 超过 {SCHEDULE_P95_MS:.0f} ms")

    stream_rss = large.get("stream_rss_delta_mb")
    if stream_rss is not None:
        ok = stream_rss <= STREAM_RSS_DELTA_MB
        rows.append(
            {
                "阈值": "单请求进程内存增量（禁止在请求路径整对象读）",
                "建议值": "与对象大小无关",
                "实测": f"流式读 {large.get('stream_read_mb')} MB → RSS {stream_rss} MB；"
                f"整对象读同量 → RSS {large.get('read_bytes_rss_delta_mb')} MB",
                "判定": "满足" if ok else "不满足",
                "备注": "整对象读那一列是反例：证明换一条路径就会线性吃内存" if ok else "流式读出现内存增量，路径可能已退化",
            }
        )
        if not ok:
            breaches.append(f"流式读 RSS 增量 {stream_rss} MB 超过 {STREAM_RSS_DELTA_MB} MB")

    rows.append(
        {
            "阈值": "队列积压可发现时延",
            "建议值": "积压且无消费者时 ≤ 1 个探测周期报 503",
            "实测": "本脚本不测",
            "判定": "由用例保证",
            "备注": "`docs/06` TC-OPS-001 已自动化三种口径",
        }
    )
    rows.append(
        {
            "阈值": "单任务端到端 P95 / 吞吐上限 / 持续时间 / 文档构成（处理阈值）",
            "建议值": "暂不设定",
            "实测": f"mock 单次执行 P95 {(queue.get('attempt_seconds') or {}).get('p95')} 秒",
            "判定": "不判定",
            "备注": "真实 OCR/LLM 慢 2–3 个数量级；要设这条需要先有真实提取器的负载模型",
        }
    )
    return {"rows": rows, "breaches": breaches}
