#!/usr/bin/env bash
# 一次跑完三项备份恢复演练（阶段 G17，docs/17 §7；TC-REC-012 + TC-REC-013）。
#
# 为什么要这个包装：G7 之前"备份演练"只有数据库那一个脚本，于是"演练通过"被读成
# "数据都有已验证的备份"，而对象卷（原文件本身）从来没被恢复验证过。两个脚本分开存在，
# 就是这种误读的土壤。这里把它们收成一个入口，并**聚合两侧结论**：任一侧不成立就非零退出，
# 绝不出现"一半绿着报通过"。
#
# 用法（Windows 侧 Git Bash）：bash scripts/run_backup_drills.sh
# 副作用要清楚：对象卷演练会把 object-store 容器 pause 数十秒，开发栈那段时间读不到对象。
set -uo pipefail
cd "$(dirname "$0")/.."

PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY="python"
DB_RC=0
OBJ_RC=0
LOG_RC=0

echo "=== 1/3 数据库备份恢复演练（在 WSL 内跑，docker CLI 只在那里） ==="
if [ "${DRILLS_SKIP:-0}" = "1" ]; then
    echo "probe: DRILLS_SKIP=1，跳过真演练（只用于验证聚合逻辑，结论会是 PROBE 而不是 PASS）"
else
    wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/d/workspace/DocPilot && bash scripts/backup_restore_drill.sh" || DB_RC=$?
fi

echo
echo "=== 2/3 对象卷冷副本恢复演练 ==="
if [ "${DRILLS_SKIP:-0}" = "1" ]; then
    echo "probe: DRILLS_SKIP=1，跳过真演练"
else
    "$PY" -X utf8 scripts/object_store_backup_drill.py || OBJ_RC=$?
fi

# 聚合本身也要能被证伪：DRILLS_PROBE=db|object 让一侧失败，证明"一半红着不会报通过"。
if [ "${DRILLS_PROBE:-}" = "db" ]; then DB_RC=1; echo "probe: 强制数据库侧失败"; fi
if [ "${DRILLS_PROBE:-}" = "object" ]; then OBJ_RC=1; echo "probe: 强制对象卷侧失败"; fi
if [ "${DRILLS_PROBE:-}" = "logical" ]; then LOG_RC=1; echo "probe: 强制逻辑级侧失败"; fi

echo
echo "=== 3/3 逻辑级对象备份恢复演练（G25，TC-REC-014） ==="
if [ "${DRILLS_SKIP:-0}" = "1" ]; then
    echo "probe: DRILLS_SKIP=1，跳过真演练"
else
    "$PY" -X utf8 scripts/object_store_logical_drill.py || LOG_RC=$?
fi

echo
echo "=== 聚合结论 ==="
if [ "${DRILLS_SKIP:-0}" = "1" ] || [ -n "${DRILLS_PROBE:-}" ]; then
    echo "DRILLS RESULT: PROBE  聚合逻辑走查，不是真演练结论"
    echo "数据库 rc=$DB_RC 对象卷 rc=$OBJ_RC 逻辑级 rc=$LOG_RC"
    [ "$DB_RC" -eq 0 ] && [ "$OBJ_RC" -eq 0 ] && [ "$LOG_RC" -eq 0 ] && exit 0
    exit 1
fi
# 三项都必须成立才报 PASS，包括那条已知间歇失败的卷级路径：
# 留一个"可选侧"就等于给聚合结论开后门，G17 要消灭的正是这个。卷级红的时候，
# 聚合就该红——那是 G23/G24 已归因的恢复侧缺陷，不是聚合器的问题。
if [ "$DB_RC" -eq 0 ] && [ "$OBJ_RC" -eq 0 ] && [ "$LOG_RC" -eq 0 ]; then
    echo "DRILLS RESULT: PASS  数据库=PASS  对象卷冷副本=PASS  逻辑级对象备份=PASS"
    echo "范围：本机、冷副本与逻辑级搬运、同机恢复。不含异机灾备、增量备份、保留策略与 RPO/RTO（G-DEC-05）"
    exit 0
fi
echo "DRILLS RESULT: FAIL  数据库=rc$DB_RC  对象卷=rc$OBJ_RC  逻辑级=rc$LOG_RC"
echo "任一侧不成立就不能报通过：按上面各自的输出定位后再重跑。"
exit 1
