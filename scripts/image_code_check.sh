#!/usr/bin/env bash
# 镜像新鲜度与标识检查的两面取证（维护轮 M19，`docs/06 §5.33` ⑤、`docs/09 §58`）。
#
# 为什么需要这个包装：`scripts/image_code_check.py` 单独跑只会给一面读数。
# 只有"干净容器里必须失败、真镜像里必须通过"两面同时成立，才证明那些判据真的在测东西，
# 而不是恒真。第一版就是因为有一面没测到，让一条判据永远返回非空字符串而没被发现。
#
# 用法（在 WSL 内，docker CLI 只存在于 Ubuntu 里）：
#   cd /mnt/d/workspace/DocPilot && bash scripts/image_code_check.sh
# 想改镜像标签：IMAGE=docpilot-app:dev bash scripts/image_code_check.sh
#
# 退出码约定：0 = 两面都符合预期；1 = 有一面不符合（镜像过期或判据坏）。
# 退出码必须在管道之外取，否则记到的是 tail/grep 的结果（`docs/09 §11` 记过这条）。
#
# 顺带记下重建镜像的配方，M18 曾在四条死路上耗掉一整轮：
#   docker build --network=host \
#       --build-arg HTTP_PROXY=http://127.0.0.1:7897 \
#       --build-arg HTTPS_PROXY=http://127.0.0.1:7897 \
#       --build-arg NO_PROXY=localhost,127.0.0.1 -t docpilot-app:dev .
# 两个条件缺一个都不成立：只 `--network=host` 时 pip 仍直连 pypi（本机实测大文件约
# 14 kB/s，pymupdf 一个包就要半小时以上）；只给代理 build-arg 时容器内的 127.0.0.1
# 是容器自己，连不上主机代理。注意"索引能通"不等于"构建能完成"——同一台机器上
# `curl https://pypi.org/simple/` 只要 0.66 秒，大文件仍是 14 kB/s。
set -uo pipefail
cd "$(dirname "$0")/.."

BASE="python:3.11-slim"
IMAGE="${IMAGE:-docpilot-app:dev}"
OUT="${OUT:-outputs}"
mkdir -p "$OUT"

# 两面各自落到文件，退出码直接来自 docker run，不经管道。
docker run --rm -v "$PWD/scripts:/check:ro" "$BASE" \
    python /check/image_code_check.py > "$OUT/image_check_clean.txt" 2>&1
clean_rc=$?
docker run --rm -v "$PWD/scripts:/check:ro" "$IMAGE" \
    python /check/image_code_check.py > "$OUT/image_check_real.txt" 2>&1
real_rc=$?

echo "clean_container_rc=$clean_rc  （期望 1：干净容器里没有本仓库代码）"
echo "image_rc=$real_rc             （期望 0：$IMAGE 里各轮标识齐全）"

# 读数本身也要能看见，否则"rc=1"分不清是判据生效还是脚本崩了。
echo "--- clean 输出:"; sed 's/^/    /' "$OUT/image_check_clean.txt"
echo "--- image 输出:"; sed 's/^/    /' "$OUT/image_check_real.txt"

if [ "$clean_rc" -ne 1 ] || [ "$real_rc" -ne 0 ]; then
    echo "IMAGE CODE CHECK BOTH-SIDES: FAIL"
    exit 1
fi
echo "IMAGE CODE CHECK BOTH-SIDES: PASS"
exit 0
