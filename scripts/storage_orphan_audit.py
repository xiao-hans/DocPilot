"""对象存储孤儿审计（docs/17 §5，TC-DATA-002 的兜底）。

只报告、不删除：保留策略 G-DEC-03 批准之前，任何脚本都无权删真实对象。
检查两条不变量：
- 存储里的每个对象键都应有 `document_versions` 记录指向它（上传补偿失败 → 孤儿对象）
- 每条记录的 `storage_key` 都应能在存储里读到（存储侧丢失 → 断链记录）

输出不含对象键与文件正文：租户与任务是内部标识，随机段用摘要代替（docs/17 §7）。
退出码 0 表示两边一致，1 表示有发现。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models.document import DocumentVersion  # noqa: E402
from app.services.object_storage import build_object_storage  # noqa: E402


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _locator(key: str) -> str:
    """把 `{tenant}/{job}/{uuid}.ext` 转成可进日志的标识：只保留内部 ID 与摘要。"""
    parts = key.split("/")
    if len(parts) >= 3:
        return f"tenant_id={parts[0]} job_id={parts[1]} key_digest={_digest(key)}"
    return f"tenant_id=? job_id=? key_digest={_digest(key)}"


def main() -> int:
    storage = build_object_storage()
    with SessionLocal() as db:
        rows = db.execute(
            select(
                DocumentVersion.storage_key,
                DocumentVersion.tenant_id,
                DocumentVersion.job_id,
                DocumentVersion.id,
            )
        ).all()

    recorded = {row.storage_key: row for row in rows}
    present = set(storage.list_keys())

    orphans = sorted(present - recorded.keys())
    broken = sorted(key for key in recorded if key not in present)

    print(
        f"audit: 后端={storage.__class__.__name__} 对象数={len(present)} 记录数={len(recorded)}"
    )
    for key in orphans:
        print(f"  孤儿对象 {_locator(key)}")
    for key in broken:
        row = recorded[key]
        print(
            f"  断链记录 tenant_id={row.tenant_id} job_id={row.job_id} "
            f"version_id={row.id} key_digest={_digest(key)}"
        )
    if orphans or broken:
        print(
            f"audit: 发现 {len(orphans)} 个孤儿对象、{len(broken)} 条断链记录"
            "（本脚本不删除任何对象，删除需 G-DEC-03 批准）"
        )
        return 1
    print("audit: 一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
