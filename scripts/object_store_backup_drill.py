"""阶段 G 收口项 R-018：对象卷的备份恢复演练（TC-REC-012 的对象侧）。

用法：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/object_store_backup_drill.py

为什么需要它：`scripts/backup_restore_drill.sh` 只覆盖 PostgreSQL。SeaweedFS 的对象不是
"一键一文件"，卷级快照恢复后必须再起一个实例才能按 S3 语义复核内容，所以在做这个演练
之前，"对象数据已有可恢复备份"这句话是没有证据的（`docs/17` G-DEC-11、`docs/08` R-018）。

证明的命题：
  1. 卷级冷副本（源容器 pause 期间复制）恢复到一个独立实例后，键集合、对象大小与
     SHA-256 内容与源一致；
  2. 校验器真的能发现三类破坏，且三类各自的检测维度互不替代（三个负向注入都必须被抓到）：
     少一个对象（只有键集合能发现）、等长改写一个字节（只有摘要能发现）、
     截断对象（只有大小能发现，摘要检查在大小不一致时无意义）；
  3. 恢复耗时与副本耗时可测量——这是将来定 RPO/RTO 的输入，不是结论。

不证明：跨机灾备、增量备份、保留策略、多副本一致性，也不证明真实生产卷的恢复时间。

安全边界（fail-closed）：
  - 只对 127.0.0.1 上的本机端点操作，端点不是本机就直接退出；
  - 源容器名与卷名必须精确等于预期值，否则拒绝；
  - 只删除本脚本创建的东西：恢复容器、WSL 内的副本目录、以及带本次随机后缀的
    `_drill-<run>/` 前缀下的探测对象；源桶里的既有对象只读不删。
  - 启动时先回收 `docpilot-object-restore-*` 容器与 `/tmp/docpilot-object-drill-*` 目录。
    这两个前缀由本脚本独占，留着它们会占住恢复端口，让下一次演练直接起不来。
  - 收尾的"已回收"必须是核对出来的，不是发完命令就声称：恢复容器、副本目录、探测对象
    三项都在删除后重新查询确认，任一项仍在即判 FAIL。

判定：`DRILL RESULT: PASS` 要求正向核对一致、探测对象摘要命中、三个负向注入
（删对象 / 等长改写 1 字节 / 截断 1 字节）分别被对应判据抓到、重建副本后复跑仍一致、
探测对象与临时容器都已回收；任何一项不成立打印 FAIL 并以退出码 1 结束。

核对按"稳定"而非"一次读数"判定：恢复实例重启后各卷是逐个向 master 注册的，刚 head 通
就去比对全部键，会把"还没稳定"读成"备份不可恢复"（本机实测 11 次第 8 步出现过 1 次不一致）。
`settle_compare` 因此在窗口内重试，并分别给出两种结论——稳定算通过并记下核对次数与耗时，
窗口耗尽仍不一致才算损坏且留下签名。就绪判定同样改用最大的探测对象，而不是一个 4 KB 小文件。

每次运行的完整转录由脚本自己落盘到 `data/drill/object-<UTC>-<run>-<PASS|FAIL|ERROR>.log`
（保留最近 20 份，只回收本脚本自己写的文件）。失败必须能事后追查，不能只留一句"某次跑挂了"。
"""

from __future__ import annotations

import builtins
import hashlib
import os
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

import boto3  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

SOURCE_CONTAINER = "docpilot-object-store"
SOURCE_VOLUME = "docpilot-dev_object-store-data"
RESTORE_CONTAINER_TPL = "docpilot-object-restore-{run}"
RESTORE_PORTS = tuple(range(18333, 18344))  # 端口被占时逐个试，绝不复用别人的端口
RESTORE_PORT = RESTORE_PORTS[0]
WSL = ["wsl.exe", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-lc"]

RUN = uuid.uuid4().hex[:8]
DRILL_PREFIX = f"docpilot/_drill-{RUN}/"
COPY_DIR = f"/tmp/docpilot-object-drill-{RUN}"
BUCKET = os.getenv("S3_BUCKET", "")
ENDPOINT = os.getenv("S3_ENDPOINT_URL", "")


def wsl(command: str) -> str:
    result = subprocess.run(WSL + [command], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"命令失败（{result.returncode}）：{command}\n{result.stdout}\n{result.stderr}")
    return (result.stdout or "").strip()


def existing_restore_containers() -> list[str]:
    """本脚本独占的恢复实例容器名。前缀是本脚本专用，出现即说明有演练没收尾干净。"""
    out = wsl("docker ps -a --format '{{.Names}}' | grep '^docpilot-object-restore-' || true")
    return sorted(name for name in out.splitlines() if name.strip())


def existing_copy_dirs() -> list[str]:
    out = wsl("ls -d /tmp/docpilot-object-drill-* 2>/dev/null || true")
    return sorted(path for path in out.splitlines() if path.strip())


TRANSCRIPT: list[str] = []


def say(*parts: object) -> None:
    """演练唯一的输出通道：既打到终端，也留在本次转录里。"""
    line = " ".join(str(part) for part in parts)
    builtins.print(line)
    TRANSCRIPT.append(line)


def write_transcript(verdict: str) -> str:
    """把整份转录自己落盘。

    只靠调用方临时重定向不算交付物：一次真实的失败如果没留下签名，就只剩下"某次跑挂了"
    这句无法追查的话（G7 交付后本机 11 次里那次第 8 步不一致就是这么丢的）。
    只回收本脚本自己写的 `object-*.log`，保留最近 20 份。
    """
    directory = BASE_DIR / "data" / "drill"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (
            f"object-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{RUN}-{verdict}.log"
        )
        path.write_text(os.linesep.join(TRANSCRIPT) + os.linesep, encoding="utf-8")
        stale = sorted(directory.glob("object-*.log"), key=lambda f: f.name)[:-20]
        for old in stale:
            old.unlink(missing_ok=True)
        return str(path)
    except OSError as exc:
        builtins.print(f"转录写入失败：{type(exc).__name__}: {exc}")
        return "(未落盘)"


def settle_compare(source, restore, keys: list[str], window_seconds: int = 90):
    """重复核对直到一致，或用尽窗口后带着最后一次签名判负。

    恢复实例重启后各卷是逐个向 master 注册的，刚 head 通就一次性比对全部键，会把"还没
    稳定"读成"备份不可恢复"。这两种结论完全不同，必须分开：稳定下来算通过并记下耗时与
    尝试次数；到窗口末尾仍不一致才算损坏，并保留签名。
    """
    started = time.monotonic()
    attempts = 0
    drift = compare(source, restore, keys)
    while any(drift):
        if time.monotonic() - started > window_seconds:
            break
        time.sleep(2.0)
        attempts += 1
        drift = compare(source, restore, keys)
    return not any(drift), attempts + 1, time.monotonic() - started, drift


def client(endpoint: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.getenv("S3_ACCESS_KEY", ""),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY", ""),
        region_name=os.getenv("S3_REGION", "us-east-1"),
    )


def guard_environment() -> list[str]:
    """宁可拒绝启动，也不能把破坏性动作落到非本机或非预期的卷上。

    返回回收掉的上一次演练残留。`docpilot-object-restore-*` 容器会占住 18333，留着它
    这一次的恢复实例根本起不来，而 `wait_ready` 只会等到超时，把状态弄得更难收拾。
    """
    if not ENDPOINT.startswith("http://127.0.0.1:") and not ENDPOINT.startswith("http://localhost:"):
        raise SystemExit(f"演练只允许本机对象端点，当前是 {ENDPOINT!r}")
    if not BUCKET:
        raise SystemExit("缺少 S3_BUCKET（见 .env.example）")
    mounts = wsl(
        f"docker inspect {SOURCE_CONTAINER} --format "
        "'{{range .Mounts}}{{.Type}} {{.Name}} {{.Destination}}{{println}}{{end}}'"
    )
    if f"volume {SOURCE_VOLUME} /data" not in mounts:
        raise SystemExit(f"源容器挂载与预期不符，拒绝继续：\n{mounts}")
    reclaimed = existing_restore_containers() + existing_copy_dirs()
    for name in existing_restore_containers():
        wsl(f"docker rm -f {name} >/dev/null 2>&1 || true")
    for path in existing_copy_dirs():
        wsl(f"rm -rf {path}")
    if reclaimed:
        say(f"0) 先回收上一次演练未收尾的残留：{reclaimed}")
        still = existing_restore_containers() + existing_copy_dirs()
        if still:
            raise SystemExit(f"上一次演练的残留无法回收，拒绝继续：{still}")
    return reclaimed


def probe_key(label: str) -> str:
    return f"{DRILL_PREFIX}{label}.bin"


def probe_objects(source):
    """写入体积跨度足够大的探测对象：只有一个 3 KB 文件时，撕裂的副本也可能碰巧比对通过。"""
    probes: dict[str, bytes] = {}
    for label, size in (("tiny", 16), ("text", 4096), ("block", 1 << 20), ("large", 5 << 20)):
        probes[probe_key(label)] = os.urandom(size)
    for key, body in probes.items():
        source.put_object(Bucket=BUCKET, Key=key, Body=body)
    return probes


def listing(s3) -> dict[str, int]:
    tokens: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET):
        for item in page.get("Contents", []):
            tokens[item["Key"]] = int(item["Size"])
    return tokens


def digest(s3, key: str) -> str:
    stream = s3.get_object(Bucket=BUCKET, Key=key)["Body"]
    hasher = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        hasher.update(chunk)
    return hasher.hexdigest()


def snapshot_copy() -> float:
    """pause 源容器后复制卷，得到一致性冷副本；unpause 后测量源端点恢复服务的耗时。"""
    wsl(f"docker pause {SOURCE_CONTAINER}")
    try:
        started = time.monotonic()
        wsl(
            f"mkdir -p {COPY_DIR} && docker run --rm "
            f"-v {SOURCE_VOLUME}:/source:ro -v {COPY_DIR}:/backup alpine "
            "sh -c 'cp -a /source/. /backup/ && du -s /backup'"
        )
        return time.monotonic() - started
    finally:
        wsl(f"docker unpause {SOURCE_CONTAINER}")


def wait_ready(endpoint: str, readable_key: str, expect_sha256: str, timeout: int = 240) -> float:
    """就绪 = 能把对象读出来**且摘要对得上**，不是桶能 head、也不是读到几个字节。

    两道都放宽过就都出过事：只 head_bucket 时卷还没向 master 注册完，第一个大对象直接
    IncompleteRead；只要求"非空"时曾拿到 0 字节响应。实测本机 SeaweedFS 恢复后约 23 秒
    才能稳定读出正确内容，所以这里必须轮询到摘要命中为止。
    """
    started = time.monotonic()
    deadline = started + timeout
    probe = client(endpoint)
    last = "未尝试"
    while time.monotonic() < deadline:
        try:
            body = probe.get_object(Bucket=BUCKET, Key=readable_key)["Body"].read()
            if hashlib.sha256(body).hexdigest() == expect_sha256:
                return time.monotonic() - started
            last = f"读到 {len(body)} 字节但摘要不符"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2.0)
    raise RuntimeError(f"恢复实例在 {timeout} 秒内读不出正确内容的 {readable_key}：{last}")


def start_restore() -> str:
    """起恢复实例并返回它的端点；端口被占就换下一个，失败时先清掉自己留下的半成品容器。

    `docker run` 失败会留下一个 Created 状态的容器并占住发布端口，第一次跑就把自己
     wedge 在那里了，所以每轮尝试前都要 rm -f 一次。
    """
    global RESTORE_PORT
    name = RESTORE_CONTAINER_TPL.format(run=RUN)
    failures: list[str] = []
    for port in RESTORE_PORTS:
        wsl(f"docker rm -f {name} >/dev/null 2>&1 || true")
        command = (
            f"docker run -d --name {name} -v {COPY_DIR}:/data "
            f"-p 127.0.0.1:{port}:8333 "
            "chrislusf/seaweedfs:4.22 server -s3 -dir=/data -ip=127.0.0.1 -ip.bind=0.0.0.0"
        )
        result = subprocess.run(WSL + [command], capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            RESTORE_PORT = port
            return f"http://127.0.0.1:{port}"
        failures.append(f":{port} → {(result.stderr or result.stdout).strip().splitlines()[-1:]}")
    raise RuntimeError(f"{len(RESTORE_PORTS)} 个候选端口都起不来恢复容器：" + "；".join(failures))


def cleanup(source=None) -> list[str]:
    """回收本次创建的一切并核对确实没了：探测对象、恢复容器、副本目录。

    只发命令不核对就会写出"已回收"这种没有证据的话——`docker rm -f ... || true` 连失败
    都会被吞掉。核对结果通过 notes 进入判定，异常路径也要走到，所以 main 的 finally 再调一次。
    """
    notes: list[str] = []
    if source is not None:
        try:
            for key in [k for k in listing(source) if k.startswith(DRILL_PREFIX)]:
                source.delete_object(Bucket=BUCKET, Key=key)
        except Exception as exc:
            notes.append(f"探测对象清理失败 {type(exc).__name__}")
    for command in (
        f"docker rm -f {RESTORE_CONTAINER_TPL.format(run=RUN)} >/dev/null 2>&1 || true",
        f"rm -rf {COPY_DIR}",
    ):
        try:
            wsl(command)
        except Exception as exc:  # 清理失败要报出来，但不能盖住主结论
            notes.append(f"清理命令失败 {type(exc).__name__}")
    if RESTORE_CONTAINER_TPL.format(run=RUN) in existing_restore_containers():
        notes.append(f"恢复容器回收后仍存在：{RESTORE_CONTAINER_TPL.format(run=RUN)}")
    if COPY_DIR in existing_copy_dirs():
        notes.append(f"副本目录回收后仍存在：{COPY_DIR}")
    return notes


def compare(source, restore, keys: list[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """返回 (源有恢复无, 恢复有源无, 大小不一致, 大小相同但摘要不一致)。

    大小与摘要必须是两个独立的判定：只在尺寸相等时才比摘要，会让"整对象被换成一小段
    内容"这种破坏同时躲过两道检查——第一次跑这个演练时就是这样漏掉的。
    """
    src, dst = listing(source), listing(restore)
    missing = sorted(k for k in keys if k not in dst)
    extra = sorted(set(dst) - set(src))
    size_mismatched: list[str] = []
    digest_mismatched: list[str] = []
    for key in keys:
        if key not in dst:
            continue
        if dst[key] != src.get(key):
            size_mismatched.append(key)
        elif digest(source, key) != digest(restore, key):
            digest_mismatched.append(key)
    return missing, extra, size_mismatched, digest_mismatched


def run_drill(source) -> int:
    restore_endpoint = ""
    probes = probe_objects(source)
    # 排除任何一次演练的探测前缀，而不只是本次：上一次被中断的演练会把它的探测对象
    # 留在这里，若按本运行后缀过滤就会把它们当成"既有应用对象"计入核对基数。
    app_keys = [key for key in listing(source)
                if key.startswith("docpilot/") and "/_drill-" not in key]
    keys = sorted(probes) + sorted(app_keys)
    expected_hashes = {key: hashlib.sha256(probes[key]).hexdigest() for key in probes}
    say(f"1) 源桶 {BUCKET}：探测对象 {len(probes)} 个 + 既有应用对象 {len(app_keys)} 个")

    copy_seconds = snapshot_copy()
    say(f"2) 卷级冷副本完成：{copy_seconds:.1f} 秒（源容器 pause 期间复制）")

    started = time.monotonic()
    restore_endpoint = start_restore()
    # 就绪必须用最大的那个探测对象来判定：小对象读通了不代表 5 MB 的那份已向 master 注册完，
    # 而后面要比对的是全部键。
    ready_seconds = wait_ready(restore_endpoint, probe_key("large"), expected_hashes[probe_key("large")])
    restore = client(restore_endpoint)
    say(f"3) 恢复实例可用：启动+就绪 {time.monotonic() - started:.1f} 秒（其中就绪 {ready_seconds:.1f} 秒）")

    verified, attempts4, settle4, drift4 = settle_compare(source, restore, keys)
    missing, extra, size_bad, digest_bad = drift4
    hash_ok = all(digest(restore, key) == expected_hashes[key] for key in probes)
    say(f"4) 正向核对：{len(keys)} 个对象，核对 {attempts4} 次、稳定耗时 {settle4:.1f} 秒，"
          f"缺 {len(missing)}、多 {len(extra)}、尺寸不符 {len(size_bad)}、摘要不符 {len(digest_bad)}，"
          f"探测对象摘要全部命中={hash_ok}")

    # 三个负向注入必须在同一份副本上依次被抓到，每个只断言自己那一类破坏。
    # 注入对象一律取自本次的探测前缀，绝不碰既有应用对象。
    absent_key = keys[0]
    restore.delete_object(Bucket=BUCKET, Key=absent_key)
    neg_missing, _, _, _ = compare(source, restore, keys)
    caught_missing = absent_key in neg_missing
    say(f"5) 负向·少对象（删除恢复侧 {absent_key.rsplit('/', 1)[-1]}）："
          f"被抓到={caught_missing} → {[k.rsplit('/', 1)[-1] for k in neg_missing]}")

    same_len_key = probe_key("text")
    flipped = bytearray(probes[same_len_key])
    flipped[0] ^= 0xFF
    restore.put_object(Bucket=BUCKET, Key=same_len_key, Body=bytes(flipped))
    _, _, neg_size, neg_digest = compare(source, restore, keys)
    caught_digest = same_len_key in neg_digest and same_len_key not in neg_size
    say(f"6) 负向·等长改写 1 字节（{same_len_key.rsplit('/', 1)[-1]}）："
          f"只有摘要能抓到={caught_digest} → {[k.rsplit('/', 1)[-1] for k in neg_digest]}")

    trunc_key = probe_key("large")
    restore.put_object(Bucket=BUCKET, Key=trunc_key, Body=probes[trunc_key][:-1])
    _, _, neg_size2, _ = compare(source, restore, keys)
    caught_size = trunc_key in neg_size2
    say(f"7) 负向·截断 1 字节（{trunc_key.rsplit('/', 1)[-1]}）："
          f"尺寸检查抓到={caught_size} → {[k.rsplit('/', 1)[-1] for k in neg_size2]}")

    # 副本已被改写，重建一份再跑正向，确认上面的抓取是注入造成的而不是校验器本身失真
    wsl(f"docker rm -f {RESTORE_CONTAINER_TPL.format(run=RUN)} >/dev/null 2>&1 || true")
    wsl(f"rm -rf {COPY_DIR}")
    snapshot_copy()
    restore_endpoint = start_restore()
    wait_ready(restore_endpoint, probe_key("large"), expected_hashes[probe_key("large")])
    restore = client(restore_endpoint)
    # 这里正是本机出现过"某次不一致"的地方：新实例重启后各卷逐个向 master 注册，比对跑得
    # 比注册快就会读成不一致。分开这两种结论——稳定下来算通过，窗口耗尽仍不一致才算损坏，
    # 并把最后一次签名留下（否则失败原因只能靠重跑撞运气）。
    rerun_ok, attempts8, settle8, drift8 = settle_compare(source, restore, keys)
    signature = "" if rerun_ok else " 签名=" + str(
        [[key.rsplit("/", 1)[-1] for key in group] for group in drift8]
    )
    say(f"8) 重建副本后复跑正向：{'一致' if rerun_ok else '窗口内始终不一致'}"
          f"（核对 {attempts8} 次、耗时 {settle8:.1f} 秒）{signature}")

    for key in probes:
        source.delete_object(Bucket=BUCKET, Key=key)
    notes = cleanup(source)
    still_probes = [key for key in listing(source) if key.startswith(DRILL_PREFIX)]
    if still_probes:
        notes.append(f"探测对象清理后仍残留 {len(still_probes)} 个")
    say(f"9) 清理并核对：" + ("探测对象、恢复容器、副本目录均已确认不存在" if not notes else "；".join(notes)))

    passed = (verified and hash_ok and caught_missing and caught_digest and caught_size
              and rerun_ok and not notes)
    say("DRILL RESULT:", "PASS" if passed else "FAIL")
    say(f"测量输入（不是结论）：副本 {copy_seconds:.1f} 秒、恢复就绪 {ready_seconds:.1f} 秒、"
          f"端到端可核对约 {copy_seconds + ready_seconds:.1f} 秒")
    # 退出码要能被门禁直接消费：结论不成立就不是 0，别让人去读最后一行才发现问题
    return 0 if passed else 1


def main() -> int:
    guard_environment()
    source = client(ENDPOINT)
    verdict = "ERROR"
    try:
        code = run_drill(source)
        verdict = "PASS" if code == 0 else "FAIL"
        return code
    except BaseException:
        # 崩在半路的那次最需要留下原因。只把 traceback 交给 stderr 是不够的：调用方一管道
        # 就没了——本机第一次复现"第 8 步之前挂掉"时就是这样又把签名丢了。
        say("本次运行抛出异常：")
        say(traceback.format_exc().rstrip())
        raise
    finally:
        # 异常路径同样要回收：上一版在这里留下过 Created 状态的容器，把端口占住，
        # 下一次演练直接起不来。清理只在 finally 里做，正常路径也不会重复删。
        for note in cleanup(source):
            say(f"清理提示：{note}")
        # 崩在半路的那次最需要把过程留下，所以落盘也放在 finally：只靠调用方临时重定向，
        # 失败签名就随终端一起丢了。
        say(f"转录已写入 {write_transcript(verdict)}")


if __name__ == "__main__":
    raise SystemExit(main())
