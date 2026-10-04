"""阶段 G20 快照退役 R3：把历史快照里的解析事实回填进规范化列（`docs/20 §3`）。

幂等（已有非空值即跳过）、只写不删、可中断重跑——与 `scripts/storage_backfill_to_object_store.py`
同一套纪律。判定逻辑在 `app/services/snapshot_retirement.py`，用例直接测那一层。

    PYTHONPATH=. .venv/Scripts/python.exe scripts/snapshot_backfill.py [--tenant <id>] [--dry-run]
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

from app.db import SessionLocal  # noqa: E402
from app.services.snapshot_retirement import backfill  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="只回填该租户的任务；默认全部")
    parser.add_argument("--dry-run", action="store_true", help="只统计会写什么，不落库")
    args = parser.parse_args()

    report = backfill(SessionLocal, tenant_id=args.tenant, dry_run=args.dry_run)
    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(
        f"R3 回填 {mode}：任务 {report.jobs_scanned} 条；"
        f"字段定义 {report.fields_filled}、进度 {report.progress_filled}、"
        f"逐文件事实 {report.file_facts_filled}、告警 {report.warnings_filled}"
    )
    print(
        f"不可判定：缺 storage_key 且同名多行 {report.ambiguous_files} 条、"
        f"快照有而版本行无 {report.snapshot_only_files} 个（这两类由 R4 单列，阻塞 R6）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
