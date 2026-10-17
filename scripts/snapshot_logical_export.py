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
- **可校验**（`--verify`）：重算每行摘要并与清单比对，任何一行被改动都会被抓到。
- **可加载**（`--replay`，维护轮 M11）：把导出写进一个**空的一次性库**再逐行读回，比对快照逐键相等。
  `--verify` 只证明"文件没被改过"——一份字段名全错但摘要自洽的导出同样能通过它；
  `--replay` 覆盖列类型、JSON 往返与主键约束，才是"这份兜底将来真能用"的判据。
- **只读**：导出过程不改任何一行、不建表、不加索引。`--replay` 只写目标一次性库。
- **落点自检**：默认输出目录在仓库**之外**（`~/docpilot-snapshot-export`），
  且脚本拒绝写入被 git 跟踪的目录——导出落到版本控制里等于把客户数据提交进仓库。

## 用法

    PYTHONPATH=. .venv/Scripts/python.exe scripts/snapshot_logical_export.py --out <dir>
    PYTHONPATH=. .venv/Scripts/python.exe scripts/snapshot_logical_export.py --verify <dir>
    PYTHONPATH=. .venv/Scripts/python.exe scripts/snapshot_logical_export.py --replay <dir> \
        --target-url postgresql+psycopg://user:pw@127.0.0.1:5432/docpilot_replay_test

`--replay` 的目标库必须先由调用方建好表结构（`alembic upgrade head`），且库名必须是一次性库。

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
PARENTS_NAME = "parents.jsonl"
MANIFEST_VERSION = 2
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


def export(out: Path, *, tenant_id: str | None = None, include_parents: bool = False) -> int:
    """导出 `snapshot` 列；`include_parents=True` 时连父行一起导出，使导出自足。

    M11 实测：只导出这一列时，把它读回数据库会撞外键——`processing_jobs.tenant_id`
    引用 `tenants`，空库里没有父行。所以"能不能独立恢复"这个问题的答案是**取决于是否连父行一起导**。
    两种模式都留着：默认最小（只保这一列，正对应门禁第 3 条要保的东西），
    `--include-parents` 得到一份能在外键开启的库上完整重建的自足导出。
    """
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / ROWS_NAME
    parents_path = out / PARENTS_NAME
    digests: list[str] = []
    count = 0
    parents: list[dict] = []

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
                    # R1 规范化列（维护轮 M13）。**导出这三列不是可选的美化**：
                    # R5 切读之后 `JobRepository._rebuild()` 的 `fields` 与 `progress`
                    # 取自这两列，不再读快照。只导 `snapshot` 的话，replay 能装进去、
                    # 但任务详情重建不出来——实测报 `缺 fields、progress`。
                    # 因此"这份导出能重建任务"要求它带上这两列。
                    "field_definitions": job.field_definitions,
                    "progress": job.progress,
                }
                handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
                digests.append(_row_digest(row))
                count += 1
        if include_parents:
            parents = _collect_parents(db, _tenants_of(rows_path), _job_ids_of(rows_path))
            with parents_path.open("w", encoding="utf-8", newline="\n") as handle:
                for parent in parents:
                    handle.write(json.dumps(parent, sort_keys=True, ensure_ascii=False) + "\n")

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_database": _source_label(),
        "table": PersistentJob.__tablename__,
        "column": "snapshot",
        "columns": ["snapshot", "field_definitions", "progress"],
        "tenant_filter": tenant_id,
        "row_count": count,
        "rows_sha256": digests,
        "rows_file": ROWS_NAME,
        "rows_file_sha256": _file_digest(rows_path),
        "include_parents": include_parents,
        "parents_file": PARENTS_NAME if include_parents else None,
        "parent_count": len(parents),
        "parents_file_sha256": _file_digest(parents_path) if include_parents else None,
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


def _tenants_of(rows_path: Path) -> set[str]:
    """从已写好的行文件里取出租户集合：父行导什么由行文件决定，不靠再查一遍库。"""
    tenants: set[str] = set()
    with rows_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                tenants.add(json.loads(line)["tenant_id"])
    return tenants


def _collect_parents(db, tenant_ids: set[str], job_ids: set[str]) -> list[dict]:
    """导出重建 `Job` 所需的全部依赖行（维护轮 M13 从"满足外键"扩到"能重建任务对象"）。

    M12 之前只导 `tenants` 与 `template_versions`——那只够让 `processing_jobs` 那一行**插得进去**。
    M13 扩到 `documents` / `document_versions` / `job_attempts` / `extraction_results`，
    因为 `JobRepository._rebuild()` 是从这些表重建 `Job` 的：版本行给 `files[]`、尝试行给
    `warnings`/`error`、结果行给 `result`。少任何一张，replay 之后任务详情都重建不出来。

    依赖顺序即列表顺序，`_insert_parents` 按 `kind` 顺序装：
    `tenants` → `documents` → `document_versions` → `job_attempts` / `extraction_results`。
    """
    from app.contexts.processing.domain import JobAttempt
    from app.contexts.review.domain import ExtractionResult
    from app.contexts.templates.domain import TemplateVersion
    from app.models.account import Tenant
    from app.models.document import Document, DocumentVersion
    from app.models.persistent_job import PersistentJob as Job

    parents: list[dict] = []
    for tenant in db.scalars(select(Tenant).where(Tenant.id.in_(sorted(tenant_ids)))).all():
        parents.append(
            {
                "kind": "tenant",
                "id": tenant.id,
                "name": tenant.name,
                "status": tenant.status,
                "created_at": _iso(tenant.created_at),
            }
        )

    # 模板版本：`processing_jobs` 与 `extraction_results` 都有指向它的外键。
    version_ids = _template_version_ids(db, job_ids)
    for version in db.scalars(
        select(TemplateVersion).where(TemplateVersion.id.in_(sorted(version_ids)))
    ).all():
        parents.append(
            {
                "kind": "template_version",
                "id": version.id,
                "template_id": version.template_id,
                "tenant_id": version.tenant_id,
                "revision": version.revision,
                "fields": version.fields,
                "published_at": _iso(version.published_at),
            }
        )

    # 文档与版本行：`_rebuild` 的 `files[]` 与"版本行不完整即 503"的判据都来自这里。
    version_rows = db.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.job_id.in_(sorted(job_ids)))
        .order_by(DocumentVersion.id)
    ).all()
    document_ids = {row.document_id for row in version_rows}
    for document in db.scalars(
        select(Document).where(Document.id.in_(sorted(document_ids))).order_by(Document.id)
    ).all():
        parents.append(
            {
                "kind": "document",
                "id": document.id,
                "tenant_id": document.tenant_id,
                "owner_id": None,
                "status": document.status,
                "doc_type": document.doc_type,
                "source": document.source,
                "retention_status": document.retention_status,
                "created_at": _iso(document.created_at),
                "updated_at": _iso(document.updated_at),
            }
        )
    for row in version_rows:
        parents.append(
            {
                "kind": "document_version",
                "id": row.id,
                "document_id": row.document_id,
                "tenant_id": row.tenant_id,
                "job_id": row.job_id,
                "revision": row.revision,
                "original_filename": row.original_filename,
                "storage_key": row.storage_key,
                "checksum": row.checksum,
                "media_type": row.media_type,
                "size_bytes": row.size_bytes,
                "page_count": row.page_count,
                "text_chars": row.text_chars,
                "ocr_applied": row.ocr_applied,
                "parse_error": row.parse_error,
                "created_at": _iso(row.created_at),
            }
        )

    # 尝试行：`_rebuild` 的 `warnings` 与 `error` 取自"当次尝试"，缺了会静默变成空告警。
    for attempt in db.scalars(
        select(JobAttempt).where(JobAttempt.job_id.in_(sorted(job_ids))).order_by(JobAttempt.id)
    ).all():
        parents.append(
            {
                "kind": "job_attempt",
                "id": attempt.id,
                "job_id": attempt.job_id,
                "tenant_id": attempt.tenant_id,
                "attempt_no": attempt.attempt_no,
                "status": attempt.status,
                "started_at": _iso(attempt.started_at),
                "finished_at": _iso(attempt.finished_at),
                "engine": attempt.engine,
                "parse_modes": attempt.parse_modes,
                "error": attempt.error,
                "warnings": attempt.warnings,
            }
        )

    # 结果行：`_rebuild` 的 `result` 由 `export_rows()` 从这张表取。
    for result in db.scalars(
        select(ExtractionResult)
        .where(ExtractionResult.job_id.in_(sorted(job_ids)))
        .order_by(ExtractionResult.id)
    ).all():
        parents.append(
            {
                "kind": "extraction_result",
                "id": result.id,
                "tenant_id": result.tenant_id,
                "job_id": result.job_id,
                "document_version_id": result.document_version_id,
                "template_version_id": result.template_version_id,
                "filename": result.filename,
                "doc_type": result.doc_type,
                "state": result.state,
                "revision_no": result.revision_no,
                "fields": result.fields,
                "values": result.values,
                "warnings": result.warnings,
                "confirmed_by": None,
                "confirmed_at": _iso(result.confirmed_at),
                "created_at": _iso(result.created_at),
                "updated_at": _iso(result.updated_at),
            }
        )
    return parents


def _template_version_ids(db, job_ids: set[str]) -> set[str]:
    from app.contexts.review.domain import ExtractionResult
    from app.models.persistent_job import PersistentJob as Job

    ids = {
        row[0]
        for row in db.execute(
            select(Job.template_version_id).where(
                Job.id.in_(sorted(job_ids)), Job.template_version_id.is_not(None)
            )
        ).all()
        if row[0]
    }
    ids |= {
        row[0]
        for row in db.execute(
            select(ExtractionResult.template_version_id).where(
                ExtractionResult.job_id.in_(sorted(job_ids)),
                ExtractionResult.template_version_id.is_not(None),
            )
        ).all()
        if row[0]
    }
    return ids


def _job_ids_of(rows_path: Path) -> set[str]:
    """从行文件取任务 ID：父行导什么由行文件决定，不靠再查一遍库。"""
    ids: set[str] = set()
    with rows_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                ids.add(json.loads(line)["id"])
    return ids


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


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


def replay(
    out: Path, target_url: str, *, rebuild_check: bool = False, rebuild_content: bool = False
) -> int:
    """把一份导出写进一个**空库**再逐行读回，证明它可加载（维护轮 M11）。

    `--verify` 只证明"文件没被改过"，证明不了"这份导出能重建出可用的行"——
    摘要算的是文件字节，一份字段名全错但摘要自洽的导出同样能通过。这里的判据是
    **加载后再读回，与文件里的快照逐键相等**：它覆盖列类型、JSON 往返与主键约束。

    目标库必须是可丢弃库（`assert_disposable_database`）：本函数会写行，写进开发库
    等于把导出内容混进真实数据。表结构必须先由调用方 `alembic upgrade head` 建好。

    ## 外键：一个实测到的约束（本函数第一次跑就撞上）

    `processing_jobs.tenant_id` 引用 `tenants`。空库里没有父行，插入直接
    `ForeignKeyViolation`。**这本身就是一条关于兜底完整性的结论**：只导出 `snapshot`
    列**不构成一次可独立恢复的备份**——它保住的是这一列的内容，把它读回数据库还需要周围的父行。
    兜底文件的价值在于"卷副本坏了时这一列还能读出来"，不在于"这份文件能单独把库建回来"。

    M12 给这个结论补上了另一面：导出时带 `--include-parents` 的话，父行在
    `parents.jsonl` 里，replay **在外键开启的情况下**也能完整重建——两条路径都留着，
    走哪条由导出文件自己决定（看清单里的 `include_parents`）。

    处理顺序刻意如此：**先尝试关掉外键强制**（PostgreSQL 的 `session_replication_role=replica`、
    SQLite 的 `PRAGMA foreign_keys=OFF`），这时一个父行都不伪造；权限不足时才回退到
    "给导出里出现的每个租户插一条最小桩行"。走了哪条路会打印出来——回退路径下目标库里
    多了导出本身没有的行，读者应当知道。
    """
    from sqlalchemy import create_engine, insert
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    from app.db import assert_disposable_database

    target_name = assert_disposable_database(target_url)
    rows_path = out / ROWS_NAME
    manifest_path = out / MANIFEST_NAME
    if not rows_path.is_file() or not manifest_path.is_file():
        print(f"缺少 {MANIFEST_NAME} 或 {ROWS_NAME}：{out} 不是一份完整导出", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = [
        json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    parents = _read_parents(out, manifest)

    dialect = make_url(target_url).drivername.split("+")[0]
    engine = create_engine(target_url)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # 有父行文件时**不动外键设置**：那正是"这份导出自足"的判据——
    # 靠关掉约束才能装进去的导出，不能算能独立恢复。
    disabled = False if parents else _disable_fk(engine, dialect)
    stubbed = 0
    loaded = 0
    mismatches: list[str] = []
    try:
        with Session() as db:
            if parents:
                # 依赖顺序是三层，不是两层：任务行自己也是被引用者。
                #
                # `job_attempts.job_id`、`extraction_results.job_id` 与
                # `document_versions.job_id` 都指向 `processing_jobs`，所以
                # "先装全部依赖行、再装任务行"会把这两类卡在外键上（M13 第一次跑就是
                # `Key (job_id)=… is not present in table "processing_jobs"`）。
                # 正确顺序：先装任务行依赖的行 → 装任务行 → 再装依赖任务行的行。
                _insert_pre_job_parents(db, parents)
            elif not disabled:
                stubbed = _stub_parents(db, records)
            for row in records:
                db.execute(
                    insert(PersistentJob).values(
                        id=row["id"],
                        tenant_id=row["tenant_id"],
                        owner_id=None,
                        status=row["status"],
                        created_at=datetime.now(timezone.utc),
                        finished_at=None,
                        attempt_count=0,
                        snapshot=row["snapshot"],
                        # R1 规范化列：不还原它们，`_rebuild` 会因为缺 fields/progress 报不完整。
                        field_definitions=row.get("field_definitions"),
                        progress=row.get("progress"),
                    )
                )
                loaded += 1
            db.flush()
            if parents:
                _insert_post_job_parents(db, parents)
            db.commit()

        with Session() as db:
            for row in records:
                stored = db.get(PersistentJob, row["id"])
                if stored is None:
                    mismatches.append(f"{row['id']}: 加载后读不到")
                elif stored.snapshot != row["snapshot"]:
                    mismatches.append(f"{row['id']}: 读回的快照与导出不一致")
    finally:
        engine.dispose()

    expected_count = int(manifest.get("row_count") or 0)
    if loaded != expected_count:
        mismatches.append(f"加载行数 {loaded} 与清单 {expected_count} 不符")
    if mismatches:
        print("REPLAY RESULT: FAIL")
        for line in mismatches[:20]:
            print(f"  - {line}")
        return 1
    if parents:
        how = f"自足导出：先装 {len(parents)} 条依赖行，外键保持开启"
    elif disabled:
        how = "外键强制关闭，未伪造任何父行"
    else:
        how = f"回退路径：为 {stubbed} 个租户插入最小桩行"
    print(f"REPLAY RESULT: PASS（{loaded} 行写入 {target_name} 后逐行读回一致；{how}）")
    if rebuild_check:
        return _rebuild_check(target_url, records, content=rebuild_content)
    return 0


def _compare_result_values(job, record: dict, db) -> list[str]:
    """逐键比 `result` 的**取值**，按复核状态分档（维护轮 M24）。

    为什么必须分档：`_rebuild` 的 `result` 来自 `export_rows()`，读的是
    `extraction_results` 的**当前值**；人工复核会更新它，而快照里存的是**机器提取时**的值。
    所以对已复核的任务，重建值与快照值**本来就应当不同**——要求相等会把正确行为判成失败。
    这类"把正确实现判红"的断言比漏报更糟：它促使人去改本来对的东西。

    分档口径取自 `extraction_results` 自己的列（**实测列名，不是 domain 里的常量名**）：
    `state` 取值 `machine`／`revised`／`confirmed`／`rejected`，配合 `revision_no`。

    - `state != 'machine'` 或 `revision_no > 1`：视为已人工修改。
      只要求"每行的键集合一致且值非空"，不比取值。
    - 其余：逐键相等。

    返回失败描述列表；空列表表示通过。
    """
    from sqlalchemy import select

    from app.contexts.review.domain import ExtractionResult

    rows = list(
        db.scalars(
            select(ExtractionResult).where(ExtractionResult.job_id == job.id)
        ).all()
    )
    human_touched = any(
        (getattr(row, "state", "machine") != "machine")
        or ((getattr(row, "revision_no", 1) or 1) > 1)
        for row in rows
    )

    snapshot_result = record["snapshot"].get("result") or []
    view_result = job.result or []
    problems: list[str] = []

    if len(view_result) != len(snapshot_result):
        # 条数不等已由"形状"那一层报过，这里不重复报，避免同一件事两条失败
        return problems

    for index, (want, got) in enumerate(zip(snapshot_result, view_result)):
        want_values = (want or {}).get("values") or {}
        got_values = (got or {}).get("values") or {}
        if human_touched:
            # 已复核：只比键集合与非空性，不比取值
            if set(want_values) != set(got_values):
                problems.append(
                    f"{job.id}: result[{index}] 键集合不一致"
                    f"（快照 {sorted(want_values)} / 重建 {sorted(got_values)}）"
                )
            elif any(value in (None, "") for value in got_values.values()):
                problems.append(
                    f"{job.id}: result[{index}] 重建出空值，{'复核过的任务不应丢值'}"
                )
        elif want_values != got_values:
            diff_keys = sorted(
                key
                for key in set(want_values) | set(got_values)
                if want_values.get(key) != got_values.get(key)
            )
            problems.append(
                f"{job.id}: result[{index}] 取值不一致，键 {diff_keys}"
                f"（快照 {[want_values.get(k) for k in diff_keys]} / "
                f"重建 {[got_values.get(k) for k in diff_keys]}）"
            )
    return problems


def _rebuild_check(target_url: str, records: list[dict], *, content: bool = False) -> int:
    """replay 之后真正调 `JobRepository._rebuild()` 重建 `Job`（维护轮 M13）。

    前两层判据各自有边界：`--verify` 只看文件字节，`--replay` 只证明行插得进去且这一列读得回。
    两者都成立时，任务详情仍可能重建不出来——`_rebuild` 需要版本行给 `files[]`、
    尝试行给 `warnings`/`error`、结果行给 `result`。所以这条判据是**把重建函数本身跑一遍**，
    并用 `Job.to_dict()` 的形状比对：重建出的对象必须能说出与非空快照一致的字段数、
    文件数与结果行数。

    比的是"形状"而不是"逐字节相等"：`_rebuild` 的产物来自规范化列，与快照不是同一份数据，
    要求相等会把判据变成"规范化列与快照必须一字不差"——那是 R4 对账的事，不是这条。

    `content=True` 时追加**取值**比对（维护轮 M24），边界见 `_compare_result_values`：
    它按复核状态分档，已复核任务不比取值。默认关闭，保持既有读数可比。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    from app.services.job_repository import IncompleteJobFacts, JobRepository

    engine = create_engine(target_url)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    repo = JobRepository()
    failures: list[str] = []
    checked = 0
    with Session() as db:
        for record in records:
            snapshot = record["snapshot"] or {}
            try:
                job = repo._rebuild(db, db.get(PersistentJob, record["id"]))
            except IncompleteJobFacts as exc:
                failures.append(f"{record['id']}: 规范化源不完整（{exc}）")
                continue
            view = job.to_dict()
            want_fields = len(snapshot.get("fields") or [])
            want_files = len(snapshot.get("files") or [])
            want_result = len(snapshot.get("result") or [])
            if want_fields and len(view["fields"]) != want_fields:
                failures.append(
                    f"{record['id']}: 重建出 {len(view['fields'])} 个字段，快照有 {want_fields} 个"
                )
            if want_files and len(view["files"]) != want_files:
                failures.append(
                    f"{record['id']}: 重建出 {len(view['files'])} 个文件，快照有 {want_files} 个"
                )
            if want_result and len(view["result"] or []) != want_result:
                failures.append(
                    f"{record['id']}: 重建出 {len(view['result'] or [])} 行结果，快照有 {want_result} 行"
                )
            if content:
                failures.extend(_compare_result_values(job, record, db))
            checked += 1
    engine.dispose()
    if failures:
        print("REBUILD RESULT: FAIL")
        for line in failures[:20]:
            print(f"  - {line}")
        return 1
    scope = "形状与取值均与快照一致" if content else "形状与快照一致"
    if content:
        # 分档会让"已复核任务的取值不同"也通过，所以文案不能说"取值都一致"。
        # 把口径印出来，避免 PASS 被读成"逐字节相等"。
        scope = "形状一致；取值按复核状态分档（未复核逐键相等，已复核只验键集与非空）"
    print(f"REBUILD RESULT: PASS（{checked} 个任务经 JobRepository._rebuild 重建，{scope}）")
    return 0


def _read_parents(out: Path, manifest: dict) -> list[dict]:
    """读父行文件；清单声明带父行但文件缺失时直接失败，不静默降级。"""
    if not manifest.get("include_parents"):
        return []
    parents_path = out / PARENTS_NAME
    if not parents_path.is_file():
        raise SystemExit(f"清单声明 include_parents，但缺少 {PARENTS_NAME}：{out} 不完整")
    return [
        json.loads(line) for line in parents_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _insert_pre_job_parents(db, parents: list[dict]) -> None:
    """装任务行之前必须就位的依赖行。顺序即依赖：被引用者先装。

    只有 `tenants` → `documents` → `template_versions` 这一层在这里。
    **`document_versions` 不在这层**：它的 `job_id` 虽然可空，但**有值**时照样受外键约束
    （M13 第二次跑就是 `Key (job_id)=… is not present in table "processing_jobs"`），
    所以它和 `job_attempts` 一样要等任务行装完。
    """
    _insert_kinds(db, parents, ("tenant", "document", "template_version"))


def _insert_post_job_parents(db, parents: list[dict]) -> None:
    """装任务行之后才能装的依赖行，仍然分两步。

    **依赖是四层，不是三层。** 任务行装完只是第二层：`document_versions.job_id` 与
    `job_attempts.job_id` 现在满足了，但 `extraction_results.document_version_id` 指向
    `document_versions`——把结果行和版本行放在同一批里装，顺序取决于列表顺序，
    而列表顺序来自导出时的查询顺序（M13 第三次跑就是
    `Key (document_version_id)=… is not present in table "document_versions"`）。
    所以再分一层：版本行与尝试行先装，结果行最后。
    """
    _insert_kinds(db, parents, ("document_version", "job_attempt"))
    _insert_kinds(db, parents, ("extraction_result",))


def _insert_kinds(db, parents: list[dict], kinds: tuple[str, ...]) -> None:
    from app.contexts.processing.domain import JobAttempt
    from app.contexts.review.domain import ExtractionResult
    from app.contexts.templates.domain import TemplateVersion
    from app.models.account import Tenant
    from app.models.document import Document, DocumentVersion

    def _dt(value: str | None):
        return datetime.fromisoformat(value) if value else None

    for kind in kinds:
        for parent in [p for p in parents if p["kind"] == kind]:
            if kind == "tenant":
                db.add(
                    Tenant(
                        id=parent["id"],
                        name=parent["name"],
                        status=parent["status"],
                        created_at=_dt(parent["created_at"]),
                    )
                )
            elif kind == "document":
                db.add(
                    Document(
                        id=parent["id"],
                        tenant_id=parent["tenant_id"],
                        owner_id=None,
                        status=parent["status"],
                        doc_type=parent["doc_type"],
                        source=parent["source"],
                        retention_status=parent["retention_status"],
                        created_at=_dt(parent["created_at"]),
                        updated_at=_dt(parent["updated_at"]),
                    )
                )
            elif kind == "document_version":
                db.add(
                    DocumentVersion(
                        id=parent["id"],
                        document_id=parent["document_id"],
                        tenant_id=parent["tenant_id"],
                        job_id=parent["job_id"],
                        revision=parent["revision"],
                        original_filename=parent["original_filename"],
                        storage_key=parent["storage_key"],
                        checksum=parent["checksum"],
                        media_type=parent["media_type"],
                        size_bytes=parent["size_bytes"],
                        page_count=parent["page_count"],
                        text_chars=parent["text_chars"],
                        ocr_applied=parent["ocr_applied"],
                        parse_error=parent["parse_error"],
                        created_at=_dt(parent["created_at"]),
                    )
                )
            elif kind == "template_version":
                db.add(
                    TemplateVersion(
                        id=parent["id"],
                        template_id=parent["template_id"],
                        tenant_id=parent["tenant_id"],
                        revision=parent["revision"],
                        fields=parent["fields"],
                        published_at=_dt(parent["published_at"]),
                    )
                )
            elif kind == "job_attempt":
                db.add(
                    JobAttempt(
                        id=parent["id"],
                        job_id=parent["job_id"],
                        tenant_id=parent["tenant_id"],
                        attempt_no=parent["attempt_no"],
                        status=parent["status"],
                        started_at=_dt(parent["started_at"]),
                        finished_at=_dt(parent["finished_at"]),
                        engine=parent["engine"],
                        parse_modes=parent["parse_modes"],
                        error=parent["error"],
                        warnings=parent["warnings"],
                    )
                )
            else:
                db.add(
                    ExtractionResult(
                        id=parent["id"],
                        tenant_id=parent["tenant_id"],
                        job_id=parent["job_id"],
                        document_version_id=parent["document_version_id"],
                        template_version_id=parent["template_version_id"],
                        filename=parent["filename"],
                        doc_type=parent["doc_type"],
                        state=parent["state"],
                        revision_no=parent["revision_no"],
                        fields=parent["fields"],
                        values=parent["values"],
                        warnings=parent["warnings"],
                        confirmed_by=None,
                        confirmed_at=_dt(parent["confirmed_at"]),
                        created_at=_dt(parent["created_at"]),
                        updated_at=_dt(parent["updated_at"]),
                    )
                )
    db.flush()


def _disable_fk(engine, dialect: str) -> bool:
    """尽量关掉外键强制；返回是否成功。失败不算错误——回退路径是插桩父行。"""
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            if dialect == "postgresql":
                conn.execute(text("SET session_replication_role = replica"))
            elif dialect == "sqlite":
                conn.execute(text("PRAGMA foreign_keys=OFF"))
            else:
                return False
        return True
    except Exception:
        # 非超级用户会在这里被拒。不打印堆栈：这是预期分支，不是异常。
        return False


def _stub_parents(db, records: list[dict]) -> int:
    """给导出里出现的每个租户插一条最小桩行，只为满足引用完整性。"""
    from app.models.account import Tenant

    existing = {row[0] for row in db.query(Tenant.id).all()}
    created = 0
    now = datetime.now(timezone.utc)
    for tenant_id in sorted({row["tenant_id"] for row in records}):
        if tenant_id in existing:
            continue
        db.add(
            Tenant(
                id=tenant_id,
                name=f"replay-stub-{tenant_id[:8]}",
                status="active",
                created_at=now,
            )
        )
        created += 1
    if created:
        db.flush()
    return created


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help=f"导出目录（默认 {DEFAULT_OUT}）")
    group.add_argument("--verify", type=Path, metavar="DIR", help="校验一份已有导出")
    group.add_argument(
        "--replay",
        type=Path,
        metavar="DIR",
        help="把导出写进一个可丢弃库再读回（需同时给 --target-url）",
    )
    parser.add_argument("--tenant", default=None, help="只导出该租户的任务；默认全部")
    parser.add_argument(
        "--include-parents",
        action="store_true",
        help="连满足引用完整性的父行（tenants、template_versions）一起导出，使导出自足",
    )
    parser.add_argument(
        "--target-url",
        default=None,
        help="--replay 的目标库连接串；库名必须是一次性库（<名称>_test 或 test_<名称>）",
    )
    parser.add_argument(
        "--rebuild-check",
        action="store_true",
        help="配合 --replay：写完之后调 JobRepository._rebuild 重建任务对象，验证详情可重建",
    )
    parser.add_argument(
        "--rebuild-content",
        action="store_true",
        help=(
            "配合 --rebuild-check：除条数外再比 result 的**取值**。"
            "已人工复核的任务只比键集合与非空性，不比取值——重建值来自 extraction_results "
            "的当前值，与快照里的机器值本来就应当不同"
        ),
    )
    args = parser.parse_args()

    if args.verify is not None:
        return verify(args.verify)
    if args.replay is not None:
        if not args.target_url:
            print("--replay 需要 --target-url（且库名必须是一次性库）", file=sys.stderr)
            return 1
        return replay(
            args.replay,
            args.target_url,
            rebuild_check=args.rebuild_check,
            rebuild_content=args.rebuild_content,
        )
    _reject_tracked_output(args.out)
    return export(args.out, tenant_id=args.tenant, include_parents=args.include_parents)


if __name__ == "__main__":
    sys.exit(main())