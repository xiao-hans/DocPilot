"""把阶段 F 之前落在本机上传目录里的存量文件回填到对象存储（G-DEC-07）。

设计约束：
- 幂等：目标已存在且校验和一致就跳过，可以中断后反复重跑。
- 带校验：`document_versions.checksum` 是权威值，每个对象都以"上传后读回内容的摘要等于它"
  为准；对不上的算失败，不会把没验证过的对象当作已迁移。
- 只写不删：源目录与数据库都不改动，所以失败最多是"还没迁完"，不会丢数据。
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

from app.platform.config import UPLOAD_DIR  # noqa: E402
from app.platform.db import SessionLocal  # noqa: E402
from app.models.document import DocumentVersion  # noqa: E402
from app.adapters.storage.object_storage import LocalStorage, build_object_storage  # noqa: E402


def _read_back_digest(storage, key: str) -> str:
    hasher = hashlib.sha256()
    with storage.open_read(key) as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _locator(version: DocumentVersion) -> str:
    return (
        f"tenant_id={version.tenant_id} job_id={version.job_id} version_id={version.id}"
    )


def main() -> int:
    target = build_object_storage()
    if isinstance(target, LocalStorage):
        sys.exit(
            "STORAGE_BACKEND 仍是 local：源与目标是同一个目录，回填没有意义。"
            "请先配好 S3 端点并把 STORAGE_BACKEND 设为 s3。"
        )
    source = LocalStorage(UPLOAD_DIR)

    copied = already_ok = failed = 0
    with SessionLocal() as db:
        versions = db.scalars(
            select(DocumentVersion).order_by(DocumentVersion.created_at, DocumentVersion.id)
        ).all()
        total = len(versions)
        for version in versions:
            key = version.storage_key
            if target.stat(key).exists:
                if _read_back_digest(target, key) == version.checksum:
                    already_ok += 1
                    continue
                print(f"  目标已存在但校验不符 {_locator(version)}")
                failed += 1
                continue
            if not source.stat(key).exists:
                print(f"  缺少源文件 {_locator(version)}")
                failed += 1
                continue
            with source.open_read(key) as stream:
                target.put(key, stream, content_type=version.media_type)
            # 读回比对同时证明了源文件内容与这次上传没有走形，不必再单独摘要源文件。
            if _read_back_digest(target, key) != version.checksum:
                print(f"  上传后读回校验不符 {_locator(version)}")
                failed += 1
                continue
            copied += 1
            if (copied + already_ok) % 50 == 0:
                print(f"backfill: 进度 {copied + already_ok}/{total}")

    print(f"backfill: 总数={total} 新复制={copied} 已在校验通过={already_ok} 失败={failed}")
    if failed:
        print("backfill: 有对象未通过校验，先不要切 STORAGE_BACKEND=s3")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
