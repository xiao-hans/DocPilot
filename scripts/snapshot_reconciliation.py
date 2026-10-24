"""阶段 G20 快照退役 R4：逐键对账，证明规范化源能完整重建（`docs/20 §3`、`§6`）。

只读：不改任何一行。它是 R6 删列的前置证据，所以判据必须能失败——三种非零情形：

- **漂移**：同一键在快照与规范化源里取值不同（含"还没回填"）。
- **仅存在于快照**：规范化源重建不出来的行（阶段 C 之前的任务、模板版本已消失、告警没有
  归属尝试行）。按 `docs/20 §4` 这条计数非零时 R6 不得执行，且**不允许**为了删列而丢这类行。
- **关联不可判定**：快照的 `files[]` 不含 `storage_key`，只能按文件名关联；同名多文件时
  谁对谁没有可靠答案，宁可挡住删除也不猜。

    PYTHONPATH=. .venv/Scripts/python.exe scripts/snapshot_reconciliation.py [--tenant <id>] [--limit N]

输出只含内部 ID 与键名，不落文件名与提取值原文（`docs/20 §6` 的转储另按审批流程做）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

from app.platform.db import SessionLocal  # noqa: E402
from app.contexts.processing.retirement import reconcile  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="只对账该租户的任务；默认全部")
    parser.add_argument("--limit", type=int, default=20, help="每类最多打印多少条明细")
    args = parser.parse_args()

    report = reconcile(SessionLocal, tenant_id=args.tenant)
    print(f"R4 对账：任务 {report.jobs_scanned} 条")
    for title, items in (
        ("漂移", report.drift),
        ("仅存在于快照（阻塞 R6）", report.snapshot_only),
        ("关联不可判定（阻塞 R6）", report.ambiguous),
    ):
        print(f"{title}：{len(items)} 条")
        for line in items[: args.limit]:
            print(f"  - {line}")
        if len(items) > args.limit:
            print(f"  …另有 {len(items) - args.limit} 条")
    if report.clean:
        print("RECONCILIATION RESULT: CLEAN（漂移 0、仅快照 0、不可判定 0）")
        return 0
    print("RECONCILIATION RESULT: NOT CLEAN —— R6 不得执行")
    return 1


if __name__ == "__main__":
    sys.exit(main())
