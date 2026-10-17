#!/usr/bin/env bash
# 提交前门禁（可选安装）：改了产品函数就提示"覆盖它的用例是否仍成立"（维护轮 M26）。
#
# ## 为什么不默认启用
#
# 仓库不替使用者改 `.git/hooks`——那是本机状态，不是仓库内容。要启用就显式装：
#
#     bash scripts/install_git_hooks.sh
#
# 卸载：删掉 `.git/hooks/pre-commit` 即可。
#
# ## 它做什么
#
# 跑 `scripts/assertion_sync_check.py --rev WORKTREE --run`：
# 从暂存/工作区改动里抽被改的产品函数，静态找出调用它们的用例，再真跑一遍。
# 只有"抽到了函数且覆盖用例失败"才阻断提交（退出码 1）；纯提示（静态那层）不阻断。
#
# ## 它不做什么
#
# - 不跑全量门禁：那要几分钟，提交时不该做。全量门禁仍在提测/交付前跑。
# - 不阻止"改了产品但没改断言内容"——静态判据看不见这件事（见脚本头注释的边界）。
# - 不联网、不改文件。
set -uo pipefail

# **不能用 `cd "$(dirname "$0")/.."`。** hook 被装在 `.git/hooks/pre-commit`，
# `$0` 是那个路径，往上一级是 `.git/` 而不是仓库根——实测因此把脚本路径拼成了
# `.git/scripts/assertion_sync_check.py`。用 git 自己定位工作区根目录。
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$ROOT" ]; then
    echo "pre-commit: 不在 Git 工作区内，跳过断言同步检查。"
    exit 0
fi
cd "$ROOT" || exit 1

PY=".venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then
    # `-x` 对符号链接会判假（venv 被链过来时常见），但那个路径其实能跑。
    # 所以这里先看**文件是否存在**，只有真的没有才回退。
    if [ -e "$PY" ]; then
        :
    elif command -v python3 >/dev/null 2>&1; then
        PY="python3"
    elif command -v python >/dev/null 2>&1; then
        PY="python"
    else
        # 静默回退到不存在的命令，会把"环境没装好"报成"用例失败"——两者处置完全不同。
        echo "pre-commit: 找不到可用的 Python（既没有 $PY，也没有 python3/python）。"
        echo "  请在仓库根目录建好 .venv，或用 git commit --no-verify 跳过本 hook。"
        exit 1
    fi
fi

# --rev WORKTREE 比工作区与 HEAD；--run 真跑覆盖用例。不加 --strict：
# 静态提示不阻断提交，只有覆盖用例真的失败才阻断。
"$PY" scripts/assertion_sync_check.py --rev WORKTREE --run
rc=$?

if [ "$rc" -ne 0 ]; then
    echo "pre-commit: 覆盖用例失败，提交已阻断。"
    echo "  若失败是既有问题而非本次改动引起，请单独修或明确记录后提交（例如 git commit --no-verify）。"
    exit 1
fi
exit 0
