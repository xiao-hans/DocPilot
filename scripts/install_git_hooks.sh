#!/usr/bin/env bash
# 安装可选的提交前门禁（维护轮 M26）。
#
# 用法（在 WSL 或 Git Bash 里，仓库根目录）：
#
#     bash scripts/install_git_hooks.sh
#
# 行为：把 `scripts/pre_commit_assertion_sync.sh` 拷成 `.git/hooks/pre-commit` 并加可执行位。
#
# ## 为什么是"显式安装"而不是直接改 .git/hooks
#
# `.git/hooks` 属本机状态、不入版本库。仓库直接往里写文件，等于替使用者改了本机配置而
# 且别人看不到——这与"密钥不得写入仓库"是同一条纪律的邻居：**仓库内容与本机状态分开**。
# 所以提供安装动作，由使用者决定是否启用。
#
# 若 `.git/hooks/pre-commit` 已存在且不是本仓库装的，脚本拒绝覆盖并打印现有内容前几行。
set -uo pipefail
cd "$(dirname "$0")/.."

HOOK_DIR=".git/hooks"
SOURCE="scripts/pre_commit_assertion_sync.sh"
TARGET="$HOOK_DIR/pre-commit"

if [ ! -d "$HOOK_DIR" ]; then
    echo "找不到 $HOOK_DIR：当前目录不是 Git 仓库？"
    exit 1
fi

if [ -e "$TARGET" ]; then
    if grep -q "维护轮 M26" "$TARGET" 2>/dev/null; then
        echo "已装过同一个 hook，直接覆盖更新。"
    else
        echo "$TARGET 已存在且不是本仓库装的，拒绝覆盖。现有内容前 5 行："
        head -5 "$TARGET" | sed 's/^/    /'
        echo "要替换请先自行备份并删除它。"
        exit 1
    fi
fi

cp "$SOURCE" "$TARGET"
chmod +x "$TARGET" 2>/dev/null || true
echo "已安装：$TARGET"
echo "卸载：rm $TARGET"
