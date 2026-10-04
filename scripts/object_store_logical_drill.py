"""R-018 的第二条路：逻辑级对象备份恢复演练（阶段 G25，TC-REC-014）。

为什么要有它：`scripts/object_store_backup_drill.py`（卷级冷副本）在 G23/G24 被证明
**副本内容与源逐文件一致，但恢复出来的实例间歇读不出对象**（12 轮 3 次，签名相同）。
也就是说"复制卷"这条路能不能恢复，取决于 SeaweedFS 能否重新服务那些字节，而不是取决于
备份本身。逻辑级复制换的是这个依赖：**逐对象从源端点读出来落到本地，再写进一个全新的
空实例，然后按 S3 语义核对**——它验的是"这些字节能不能重建出一个可用的库"。

用法：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/object_store_logical_drill.py

它证明什么：
  1. 备份集（本地文件 + 清单）足以在新实例上重建出与源一致的键集合、尺寸与 SHA-256；
  2. 核对判据能失败：等长改写一个字节必须被摘要检查抓到（改坏→报漂移→复原→再核对干净）；
  3. 备份耗时与"重建 + 稳定"耗时可测量——这是定 RPO/RTO 的输入，不是结论。

它不证明什么：跨机灾备、增量备份、保留策略、生产数据量下的时间；也不推翻卷级演练的结论，
两条路各自给出各自的可重复性。

安全边界沿用卷级演练：只对 127.0.0.1 上的本机端点操作；容器名/卷名必须精确匹配；
只删自己创建的东西（恢复容器、`/tmp/docpilot-object-drill-<run>`、本地备份目录、
带本次后缀的 `_drilllog-<run>/` 前缀），源桶既有对象只读不删；"已回收"以重新查询为准。
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(BASE_DIR / "scripts"))

import object_store_backup_drill as volume  # noqa: E402

RUN = uuid.uuid4().hex[:8]
# 复用卷级演练的机器（恢复容器启动、核对、回收、转录），但换成这一条路自己的运行标识。
volume.RUN = RUN
volume.DRILL_PREFIX = f"docpilot/_drilllog-{RUN}/"
volume.COPY_DIR = f"/tmp/docpilot-object-drill-{RUN}"
BACKUP_DIR = BASE_DIR / "data" / "drill" / f"logical-{RUN}"


def backup_objects(source, keys: list[str]) -> tuple[float, dict[str, tuple[int, str]]]:
    """把每个对象从源端点读出来落到本地，并记下 (字节数, SHA-256) 作为清单。

    清单是"备份集自证"的那一半：没有它，恢复后只能拿源当尺子，源一旦不可用就没法核对。
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, tuple[int, str]] = {}
    started = time.monotonic()
    for key in keys:
        body = source.get_object(Bucket=volume.BUCKET, Key=key)["Body"].read()
        digest = hashlib.sha256(body).hexdigest()
        path = BACKUP_DIR / key.replace("/", "__")
        path.write_bytes(body)
        manifest[key] = (len(body), digest)
    return time.monotonic() - started, manifest


def put_objects(restore, manifest: dict[str, tuple[int, str]]) -> float:
    """按清单把备份集写进实例。建桶与写对象分开：重放单个对象时桶已经在了。"""
    started = time.monotonic()
    for key in manifest:
        body = (BACKUP_DIR / key.replace("/", "__")).read_bytes()
        restore.put_object(Bucket=volume.BUCKET, Key=key, Body=body)
    return time.monotonic() - started


def wait_writable(endpoint: str, timeout: int = 240) -> float:
    """等恢复实例真的能服务 S3 写请求，而不是端口一通就开始。

    第一版没有这一步，5 轮里 2 轮在建桶上抛 ConnectionClosedError——那是夹具把
    "实例还没起来"读成了"备份不可用"，与备份内容无关。卷级演练早就用 wait_ready
    踩过这个坑，这里补上写侧的等价物：建桶幂等重试（已存在算成功），最多等 timeout 秒。
    """
    restore = volume.client(endpoint)
    started = time.monotonic()
    last = "未尝试"
    while time.monotonic() - started < timeout:
        try:
            restore.create_bucket(Bucket=volume.BUCKET)
            return time.monotonic() - started
        except Exception as exc:
            name = type(exc).__name__
            if "AlreadyExists" in name or "AlreadyOwned" in name:
                return time.monotonic() - started
            last = f"{name}: {exc}"
        time.sleep(2.0)
    raise RuntimeError(f"恢复实例在 {timeout} 秒内不可写：{last}")


def create_bucket(restore) -> None:
    """桶已由 wait_writable 建好；这里只兜住"中途桶又没了"这种真失败。"""
    try:
        restore.create_bucket(Bucket=volume.BUCKET)
    except Exception as exc:
        if "AlreadyExists" in type(exc).__name__ or "AlreadyOwned" in type(exc).__name__:
            return
        raise RuntimeError(f"恢复实例建桶失败：{type(exc).__name__}: {exc}") from exc


def corrupt_one(manifest: dict[str, tuple[int, str]]) -> str:
    """等长改写一个字节：只有摘要能抓到这一类，尺寸检查在它面前是瞎的。"""
    key = next(iter(manifest))
    path = BACKUP_DIR / key.replace("/", "__")
    data = bytearray(path.read_bytes())
    data[0] ^= 0x01
    path.write_bytes(bytes(data))
    return key


def main() -> int:
    volume.say(f"逻辑级对象备份恢复演练 run={RUN}")
    problems = volume.guard_environment()
    for problem in problems:
        volume.say("  环境不符：", problem)
    if problems:
        volume.write_transcript("ERROR")
        return 1

    source = volume.client(volume.ENDPOINT)
    restore_endpoint = ""
    passed = False
    backup_seconds = 0.0
    try:
        probes = volume.probe_objects(source)
        # 只备自己的探测对象，证明的是脚本能跑；把桶里已有的应用对象一起纳入，才证明
        # 「客户资产可以被重建」。它们只读不删，回收时不碰。
        existing = sorted(
            (key for key in volume.listing(source) if not key.startswith("docpilot/_drill")),
            key=lambda key: -volume.listing(source)[key],
        )[:2]
        keys = sorted(set(probes) | set(existing))
        volume.say(
            f"1) 探测对象已写入源桶：{len(probes)} 个（前缀 {volume.DRILL_PREFIX}）；"
            f"另纳入既有应用对象 {len(existing)} 个（只读）"
        )

        backup_seconds, manifest = backup_objects(source, keys)
        volume.say(
            f"2) 备份集写出：{len(manifest)} 个对象 / "
            f"{sum(size for size, _ in manifest.values())} 字节，{backup_seconds:.1f} 秒"
        )

        restore_endpoint = volume.start_restore()
        restore = volume.client(restore_endpoint)
        writable_seconds = wait_writable(restore_endpoint)
        create_bucket(restore)
        put_seconds = put_objects(restore, manifest)
        volume.say(
            f"3) 空实例就绪并重建完成：{restore_endpoint}"
            f"（可写等待 {writable_seconds:.1f} 秒、写入 {put_seconds:.1f} 秒）"
        )

        stable, attempts, settle_seconds, drift = volume.settle_compare(source, restore, keys)
        volume.say(
            f"4) 源与恢复实例核对：{'一致' if stable else '不一致'}"
            f"（{attempts} 次核对 / {settle_seconds:.1f} 秒）"
        )
        for line in (drift[0] + drift[1] + drift[2] + drift[3])[:8]:
            volume.say("   - ", line)
        if not stable:
            raise RuntimeError("恢复实例与源核对不一致，逻辑级备份不可用")

        bad_key = corrupt_one(manifest)
        put_objects(restore, {bad_key: manifest[bad_key]})
        still_drift = volume.compare(source, restore, [bad_key])
        caught = bool(still_drift[3])
        volume.say(
            f"5) 负向（等长改写 {bad_key.rsplit('/', 1)[-1]} 一个字节）："
            f"{'被摘要检查抓到' if caught else '没被抓到——判据失效'}"
        )
        if not caught:
            raise RuntimeError("摘要核对没能抓到等长改写，这条演练没有判定能力")
        # 复原：把清单里的原始字节重新写回去，再核对一次，证明第 5 步的漂移来自注入而非实例。
        path = BACKUP_DIR / bad_key.replace("/", "__")
        original = source.get_object(Bucket=volume.BUCKET, Key=bad_key)["Body"].read()
        path.write_bytes(original)
        restore.put_object(Bucket=volume.BUCKET, Key=bad_key, Body=original)
        restored_stable, _, _, _ = volume.settle_compare(source, restore, [bad_key])
        volume.say(f"6) 注入复原后复跑核对：{'一致' if restored_stable else '不一致'}")
        if not restored_stable:
            raise RuntimeError("复原后仍不一致，注入没有真的被撤销")

        passed = True
    except Exception:
        volume.say("本次运行抛出异常：\n" + traceback.format_exc())
    finally:
        leftovers = volume.cleanup(source)
        for path in sorted(BACKUP_DIR.glob("*")) if BACKUP_DIR.exists() else []:
            path.unlink(missing_ok=True)
        try:
            BACKUP_DIR.rmdir()
        except OSError:
            pass
        if leftovers:
            volume.say("  回收后仍存在（判负）：", "；".join(leftovers))
        passed = passed and not leftovers
        volume.say(f"测量输入（不是结论）：备份 {backup_seconds:.1f} 秒；备份集落在 {BACKUP_DIR.name}")
        volume.say("LOGICAL DRILL RESULT:", "PASS" if passed else "FAIL")
        volume.write_transcript("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
