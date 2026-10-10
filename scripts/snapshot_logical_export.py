"""快照退役 R6 的纯逻辑导出兜底（`docs/20 §6` 门禁第 3 条）。

## 它解决什么

R6 要 `ALTER TABLE processing_jobs DROP COLUMN snapshot`，**不可逆**：出问题只能从备份恢复库。
卷级冷副本不保证每次可恢复（R-018：12 轮 3 次失败、根因未归因），所以删列前必须**另存一份纯逻辑导出**
——把 `snapshot` 列的内容逐行读出来写成自包含文件，落到版本控制之外的独立位置。
卷副本坏了，这份导出仍然可读；它不依赖任何数据库或存储后端。

## 为什么不是 `pg_dump --column`

`docs/20 §6` 原话是"`pg_dump --column` 级别的 `processing_jobs` 快照转储"。真要按列转储，
`pg_dump` 没有这个粒度：它要么整库、要么整表。所以这里的做法是**逐行读 `snapshot` 列并序列化**，
效果等价而更小——导出文件里只有这一列加主键与租户，不含任何其他表的内容。
这也让它能在 SQLite 与 PostgreSQL 上跑同一份代码。

## 判据

- **自包含**：导出目录里有 `manifest.json`（行数、逐行摘要、源库标识、时间、列名）与
  `snapshot.jsonl`（每行一条 JSON）。有清单才能事后证明"这份导出覆盖了当时库里的每一行"。
- **可校验**：`--verify` 重算每行摘要并与清单比对，任何一行被改动都会被抓到。
- **只读**：导出过程不改任何一行、不建表、不加索引。
- **落点自检**：默认输出目录在仓库**之外**（`~/docpilot-snapshot-export`），
  且脚本拒绝写入被 git 跟踪的目录——导出落到版本控制里等于把客户数据提交进仓库。

## 用法

    PYTHONPATH=. .venv/Scripts/python.exe scripts/snapshot_logical_export.py --out <dir>
    PYTHONPATH=. .venv/Scripts/python.exe scripts/snapshot_logical_export.py --verify <dir>

**这不是删列授权**。它只是把门禁第 3 条里"另存兜底"这一步做成可执行的工具；
R6 仍受 `docs/20 §6` 四条门禁约束，其中第 4 条（负责人书面确认）未满足。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

from sqlalchemy import select  # noqa: E402

from app.config import DATABASE_URL  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.persistent_job import PersistentJob  # noqa: E402

MANIFEST_NAME = "manifest.json"
ROWS_NAME = "snapshot.jsonl"
MANIFEST_VERSION = 1
# 默认落在仓库之外：导出含客户数据，进版本控制就是事故。
DEFAULT_OUT = Path.home() / "docpilot-snapshot-export"


def _canonical(row: dict) -> bytes:
    """一行的规范字节：键序固定、不转义非 ASCII。

    `sort_keys=True` 是必须的：同一个 dict 在两次序列化里键序可能不同（PostgreSQL 的 JSON
    与 Python dict 都不保证），键序一变摘要就变，校验会误报"被改动"。
    """
    return json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _row_digest(row: dict) -> str:
    return hashlib.sha256(_canonical(row)).hexdigest()


def _reject_tracked_output(out: Path) -> None:
    """拒绝写入仓库工作树内的目录：导出的内容属于客户数据，不应进版本控制。"""
    try:
        out.resolve().relative_to(BASE_DIR.resolve())
    except ValueError:
        return  # 在仓库外，正常
    raise SystemExit(
        f"输出目录在仓库工作树内（{out}）：导出含客户数据，请改到仓库之外，"
        f"例如默认的 {DEFAULT_OUT}"
    )


def export(out: Path, *, tenant_id: str | None = None) -> int:
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / ROWS_NAME
    digests: list[str] = []
    count = 0

    with SessionLocal() as db:
        stmt = select(PersistentJob).order_by(PersistentJob.id)
        if tenant_id:
            stmt = stmt.where(PersistentJob.tenant_id == tenant_id)
        with rows_path.open("w", encoding="utf-8", newline="\n") as handle:
            for job in db.scalars(stmt).all():
                row = {
                    "id": job.id,
                    "tenant_id": job.tenant_id,
                    "status": job.status,
                    # 刻意**不**清洗 NUL（与入库边界那套相反）。
                    #
                    # 入库时剔除 NUL 是因为 PostgreSQL 的 text/json 拒收它、整笔事务会回滚；
                    # 这里是导出，目的正是"将来能原样读回来"。实测 `json.dumps` 会把 NUL
                    # 转义成 `\u0000`（文件里没有裸 NUL），`json.loads` 能无损还原，
                    # 所以清洗只会丢数据而不会提高可读性——一份被改过的兜底导出比没有更危险，
                    # 因为它看起来是完整的。
                    "snapshot": job.snapshot,
                }
                handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
                digests.append(_row_digest(row))
                count += 1

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_database": _source_label(),
        "table": PersistentJob.__tablename__,
        "column": "snapshot",
        "tenant_filter": tenant_id,
        "row_count": count,
        "rows_sha256": digests,
        "rows_file": ROWS_NAME,
        "rows_file_sha256": _file_digest(rows_path),
        "note": (
            "R6 删列门禁第 3 条的兜底导出（docs/20 §6）。它不是删列授权："
            "门禁四条需同时成立，其中负责人书面确认未满足。"
        ),
    }
    (out / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"导出完成：{count} 行 → {rows_path}")
    print(f"清单：{out / MANIFEST_NAME}")
    print(f"源库：{manifest['source_database']}")
    return 0


def verify(out: Path) -> int:
    manifest_path = out / MANIFEST_NAME
    rows_path = out / ROWS_NAME
    if not manifest_path.is_file() or not rows_path.is_file():
        print(f"缺少 {MANIFEST_NAME} 或 {ROWS_NAME}：{out} 不是一份完整导出", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected: list[str] = list(manifest.get("rows_sha256") or [])
    actual: list[str] = []
    with rows_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                actual.append(_row_digest(json.loads(line)))

    failures: list[str] = []
    if len(actual) != len(expected):
        failures.append(f"行数不符：清单 {len(expected)}，实际 {len(actual)}")
    for index, (want, got) in enumerate(zip(expected, actual, strict=False)):
        if want != got:
            failures.append(f"第 {index} 行摘要不符（内容被改动或顺序变了）")
    file_digest = _file_digest(rows_path)
    if manifest.get("rows_file_sha256") and manifest["rows_file_sha256"] != file_digest:
        failures.append("整文件摘要不符")

    if failures:
        print("VERIFY RESULT: FAIL")
        for line in failures[:20]:
            print(f"  - {line}")
        if len(failures) > 20:
            print(f"  …… 共 {len(failures)} 条")
        return 1
    print(
        f"VERIFY RESULT: PASS（{len(actual)} 行逐行摘要一致，"
        f"整文件摘要 {file_digest[:12]}…）"
    )
    return 0


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_label() -> str:
    """源库标识：只保留方言与库名，不落主机、用户名与密码。"""
    from sqlalchemy.engine import make_url

    url = make_url(DATABASE_URL)
    return f"{url.drivername}://{url.database or '(default)'}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help=f"导出目录（默认 {DEFAULT_OUT}）")
    group.add_argument("--verify", type=Path, metavar="DIR", help="校验一份已有导出")
    parser.add_argument("--tenant", default=None, help="只导出该租户的任务；默认全部")
    args = parser.parse_args()

    if args.verify is not None:
        return verify(args.verify)
    _reject_tracked_output(args.out)
    return export(args.out, tenant_id=args.tenant)


if __name__ == "__main__":
    sys.exit(main())