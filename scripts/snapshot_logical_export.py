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
                }
                handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
                digests.append(_row_digest(row))
                count += 1
        if include_parents:
            parents = _collect_parents(db, {row_tenant for row_tenant in _tenants_of(rows_path)})
            with parents_path.open("w", encoding="utf-8", newline="\n") as handle:
                for parent in parents:
                    handle.write(json.dumps(parent, sort_keys=True, ensure_ascii=False) + "\n")

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


def _collect_parents(db, tenant_ids: set[str]) -> list[dict]:
    """导出满足引用完整性所需的最小父行集合。

    只导 `tenants`：`processing_jobs` 的另一条外键是
    `(tenant_id, template_version_id) → template_versions`，而该列**可空**——
    导出的行里凡是带模板版本的，都要连对应版本一起导，否则外键仍会拒绝。
    """
    from app.contexts.templates.domain import TemplateVersion
    from app.models.account import Tenant

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
    # 行文件里出现过的模板版本：不导会让带模板版本的行走不进外键。
    version_ids = {
        version_id
        for version_id in _template_version_ids(db, tenant_ids)
    }
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
    return parents


def _template_version_ids(db, tenant_ids: set[str]) -> set[str]:
    from app.models.persistent_job import PersistentJob as Job

    rows = db.execute(
        select(Job.template_version_id).where(
            Job.tenant_id.in_(sorted(tenant_ids)), Job.template_version_id.is_not(None)
        )
    ).all()
    return {row[0] for row in rows if row[0]}


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


def replay(out: Path, target_url: str) -> int:
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
                _insert_parents(db, parents)
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
                    )
                )
                loaded += 1
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
        how = f"自足导出：先装 {len(parents)} 条父行，外键保持开启"
    elif disabled:
        how = "外键强制已关闭，未伪造任何父行"
    else:
        how = f"回退路径：为 {stubbed} 个租户插入最小桩行"
    print(f"REPLAY RESULT: PASS（{loaded} 行写入 {target_name} 后逐行读回一致；{how}）")
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


def _insert_parents(db, parents: list[dict]) -> None:
    """按父行文件重建 `tenants` 与 `template_versions`。顺序固定：父的父先装。"""
    from app.contexts.templates.domain import TemplateVersion
    from app.models.account import Tenant

    for kind in ("tenant", "template_version"):
        for parent in [p for p in parents if p["kind"] == kind]:
            if kind == "tenant":
                db.add(
                    Tenant(
                        id=parent["id"],
                        name=parent["name"],
                        status=parent["status"],
                        created_at=datetime.fromisoformat(parent["created_at"]),
                    )
                )
            else:
                db.add(
                    TemplateVersion(
                        id=parent["id"],
                        template_id=parent["template_id"],
                        tenant_id=parent["tenant_id"],
                        revision=parent["revision"],
                        fields=parent["fields"],
                        published_at=(
                            datetime.fromisoformat(parent["published_at"])
                            if parent["published_at"]
                            else None
                        ),
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
    args = parser.parse_args()

    if args.verify is not None:
        return verify(args.verify)
    if args.replay is not None:
        if not args.target_url:
            print("--replay 需要 --target-url（且库名必须是一次性库）", file=sys.stderr)
            return 1
        return replay(args.replay, args.target_url)
    _reject_tracked_output(args.out)
    return export(args.out, tenant_id=args.tenant, include_parents=args.include_parents)


if __name__ == "__main__":
    sys.exit(main())