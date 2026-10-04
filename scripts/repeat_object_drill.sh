#!/usr/bin/env bash
# 阶段 G23/G25：对象存储备份恢复演练的可重复性取证（TC-REC-013 的判据要求"同一次演练需可重复通过"）。
# 每轮单独记退出码与结论行；退出码必须在管道之外取，否则记到的会是 grep 的结果。
set -uo pipefail
cd "$(dirname "$0")/.."

PY=".venv/Scripts/python.exe"
# DRILL=volume（卷级冷副本，默认）| logical（逻辑级对象备份，G25）
DRILL="${DRILL:-volume}"
[ "$DRILL" = "logical" ] && SCRIPT=scripts/object_store_logical_drill.py || SCRIPT=scripts/object_store_backup_drill.py
[ -x "$PY" ] || PY="python"
RUNS="${RUNS:-5}"
pass=0
fail=0
for i in $(seq 1 "$RUNS"); do
    out="$(mktemp)"
    "$PY" -X utf8 "$SCRIPT" >"$out" 2>&1
    rc=$?
    echo "=== run $i rc=$rc ==="
    grep -a "DRILL RESULT\|副本\|就绪\|回收\|失败" "$out" | tail -8
    if [ "$rc" -eq 0 ]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "--- 失败轮次的完整输出（用于归因，不省略） ---"
        tail -40 "$out"
    fi
    rm -f "$out"
done
echo "=== 汇总：$RUNS 轮，通过 $pass，失败 $fail ==="
[ "$fail" -eq 0 ] && echo "REPEAT RESULT: PASS" || echo "REPEAT RESULT: FAIL"
