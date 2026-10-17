# -*- coding: utf-8 -*-
"""断言同步门禁：产品函数体被改，而覆盖它的用例一个都没动时给出提示（维护轮 M25）。

## 它要拦的那类事

M1 发现"`list_jobs` 同刻度顺序无保证"，改法是给排序键加 `id`——**产品改了，旧断言没改**。
那条旧断言要求"按创建顺序倒序"，在新排序键下只剩约 1/6 偶然通过率，之后必然红
（详见 `docs/06` TC-FUNC-070、`docs/09 §62`）。这类账的代价是几周后有人在别处撞红，
而且第一反应会是"产品坏了"，不是"断言过时了"。

## 判据

1. 从 `git diff -U0 <base>..<rev>` 取**产品文件**（`app/`、`scripts/`）里被改动的函数。
   函数名取自 hunk 头上下文（`@@ ... def foo`）与 hunk 内 `def foo(` 的新增/删除行——
   改函数体内部一行时 hunk 头不一定带名字，所以两处都看。
2. 用 AST 静态扫 `tests/`，找出**调用了**这些函数名的测试用例（`obj.foo(...)` 或 `foo(...)`）。
3. 若某个产品函数有测试调用它，而**这些测试一个都没在这次提交里被改**，报一条提示。

## 它不做什么

- **不判对错，只提示**。产品函数被改而测试没动，常常是正当的（内部重构、性能改写、
  新增分支但旧断言仍成立）。所以本门禁默认退出码 0，用 `--strict` 才把提示当失败。
- 不做动态覆盖率：那需要跑全量套件，成本与本门禁的目的（提交时快速提示）不匹配。

## 已知边界（必须写出来，否则会被过度信任）

- **只在"函数体被改"时生效**。纯常量改动、模块级语句改动抽不到函数名，不报。
- **按名字匹配调用**，同名的不同函数会互相误配（本仓库里 `list_jobs` 在
  `job_manager` 与 `processing` 各有一个，属已知情形）。
- **看不到间接调用**：`getattr`、反射、DI 容器注入的调用不产生 AST 属性访问。
- **最要紧的一条：它只能看见"测试文件被动过"，看不见"断言是否仍然成立"。**
  实测靶子就是反例：M1 那次 `list_jobs` 的排序键改了，也**动了**
  `test_list_jobs_reverse_chronological` 这个文件与用例——但只是把它跑了一遍，
  断言内容一字未改，于是旧断言在新键下必然红。本门禁对这次改动报 PASS。
  换句话说：**它拦得住"完全忘了看测试"，拦不住"看了但没改对"**。
- 因此它是**提示器**，不是"断言必然同步"的证明。真正的判据仍是跑测试：
  改了产品行为之后，把覆盖该函数的用例跑一遍并看结果。

用法：

    .venv/Scripts/python.exe scripts/assertion_sync_check.py            # 查 HEAD 上一次提交
    .venv/Scripts/python.exe scripts/assertion_sync_check.py --rev <sha>
    .venv/Scripts/python.exe scripts/assertion_sync_check.py --run      # 直接跑覆盖用例（强判据）
    .venv/Scripts/python.exe scripts/assertion_sync_check.py --strict    # 有提示即非零退出

`--run` 与静态提示的关系：静态那一层只能提示"测试文件没动"，实测拦不住"动了但没改对"
（M1 就是）。`--run` 把覆盖改动函数的用例**真跑一遍**，那是唯一能证明断言仍成立的判据；
代价是要等测试跑完，所以默认关闭。
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import subprocess
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
PRODUCT_PREFIXES = ("app/", "scripts/")
# 新文件侧的 hunk 头：`@@ -a,b +c,d @@ ...`，取 c 与 d
HUNK_NEW = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=BASE_DIR, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout


def changed_line_numbers(diff: str) -> set[int]:
    """从 diff 的 hunk 头取**新文件**里被覆盖的起始行与长度，展开成行号集合。

    只取 `+` 侧（`@@ -a,b +c,d @@` 的 `c`）：改的是新文件里的位置。
    纯删除时 `d=0`，取 `c` 处仍落在原函数体内。
    """
    lines: set[int] = set()
    for line in diff.splitlines():
        m = HUNK_NEW.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count == 0:
            lines.add(max(1, start))
        else:
            lines.update(range(start, start + count))
    return lines


def functions_covering(source: str, lines: set[int]) -> set[str]:
    """用 AST 找出源码里覆盖这些行号的函数/方法名。

    为什么用行号而不是 hunk 上下文：实测两次抽空——git 的 hunk 头常常只给类名，
    而 `-U3` 的上下文也未必包含 `def`（函数体超过 3 行时，改中间一行，`def` 不在 hunk 里）。
    行号来自 git 自己算的 hunk 头，再用 AST 算函数范围，这两步都是确定性的。
    """
    if not lines:
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None:
            continue
        if any(node.lineno <= ln <= end for ln in lines):
            names.add(node.name)
    return names


def changed_functions(diff: str, source: str) -> set[str]:
    """diff 覆盖到的函数名（行号 + AST，不看 hunk 上下文）。"""
    return functions_covering(source, changed_line_numbers(diff))


def tests_calling(names: set[str]) -> dict[str, set[tuple[str, str]]]:
    """静态扫 tests/，返回 `函数名 -> {(测试文件相对路径, 用例名)}`。"""
    wanted = set(names)
    found: dict[str, set[tuple[str, str]]] = {}
    for path in sorted((BASE_DIR / "tests").glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(BASE_DIR).as_posix()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            used: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr in wanted:
                    used.add(sub.attr)
                elif isinstance(sub, ast.Name) and sub.id in wanted:
                    used.add(sub.id)
            for name in used:
                found.setdefault(name, set()).add((rel, node.name))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="产品函数体被改而覆盖它的用例未动时给出提示")
    parser.add_argument("--rev", default="HEAD", help="检查哪次提交，默认 HEAD")
    parser.add_argument("--strict", action="store_true", help="有提示即以非零退出")
    parser.add_argument(
        "--run",
        action="store_true",
        help="把覆盖改动函数的用例真跑一遍（强判据：能证明断言在改动后仍成立）",
    )
    args = parser.parse_args()

    rev = args.rev
    if args.rev == "WORKTREE":
        # 提交前用法：比工作区（含已暂存）与 HEAD。这也是唯一能做反向验证的入口——
        # 历史提交的代码已不在工作区，`--run` 只能对着当前代码跑。
        diff_range = ["HEAD"]
    else:
        diff_range = [f"{args.rev}~1", args.rev]

    files = [f for f in git("diff", "--name-only", *diff_range).splitlines() if f.strip()]
    product = [f for f in files if f.startswith(PRODUCT_PREFIXES) and f.endswith(".py")]
    touched_tests = {f for f in files if f.startswith("tests/") and f.endswith(".py")}

    if not product:
        print(f"ASSERTION SYNC: 提交 {rev} 未改产品代码，无需检查")
        return 0

    prod_funcs: dict[str, set[str]] = {}
    for path in product:
        # 必须用 -U3（默认值）：只改函数体内部一行时，函数名只作为上下文行出现，
        # 而 `-U0` 不输出上下文，会把它整条丢掉。
        # 用**当前工作区**的文件内容算函数范围：行号来自 diff 的新文件侧。
        # 历史提交的检查因此有个已知偏差：若该文件在之后又被改过，行号会对不上。
        source = (BASE_DIR / path).read_text(encoding="utf-8") if (BASE_DIR / path).exists() else ""
        names = changed_functions(git("diff", "-U3", *diff_range, "--", path), source)
        if names:
            prod_funcs[path] = names

    if not prod_funcs:
        print(f"ASSERTION SYNC: 提交 {rev} 未抽到被改的函数（可能只改了常量或模块级语句）")
        return 0

    touched_test_funcs: set[str] = set()
    for path in sorted(touched_tests):
        test_source = (BASE_DIR / path).read_text(encoding="utf-8") if (BASE_DIR / path).exists() else ""
        touched_test_funcs |= changed_functions(
            git("diff", "-U3", *diff_range, "--", path), test_source
        )

    hints: list[str] = []
    covering_tests: set[tuple[str, str]] = set()
    for path, names in sorted(prod_funcs.items()):
        callers = tests_calling(names)
        for name in sorted(names):
            covering = callers.get(name, set())
            if not covering:
                continue  # 没有任何测试调用它，本门禁无从判断
            covering_tests |= covering
            if any(test in touched_test_funcs for _, test in covering):
                continue  # 有覆盖它的用例被改过（注意：改过不等于改对，见模块头边界）
            names_cn = "、".join(sorted(test for _, test in covering)[:3])
            hints.append(f"{path}::{name} 改了，覆盖它的 {len(covering)} 条用例都没动：{names_cn}")

    print(f"ASSERTION SYNC: 提交 {rev}，产品文件 {len(product)} 个，改动函数 {sum(len(v) for v in prod_funcs.values())} 个")

    run_failed = False
    if args.run and covering_tests:
        node_ids = [f"{path}::{test}" for path, test in sorted(covering_tests)]
        print(f"ASSERTION SYNC: --run 执行 {len(node_ids)} 条覆盖用例（这是唯一能证明断言仍成立的判据）")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *node_ids, "-p", "no:cacheprovider", "-q"],
            cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        tail = [ln for ln in (result.stdout or "").splitlines() if ln.strip()][-3:]
        for ln in tail:
            print(f"  {ln.strip()[:110]}")
        run_failed = result.returncode != 0
        # 区分"用例失败"（rc=1）与"pytest 自己没跑起来"（rc=2/3/4）。两者处置不同：
        # 前者要去看断言，后者要看环境（例如 venv 不可达）。实测副本环境里出现过
        # rc=4 + `WinError 1920`，那是环境问题，不是断言问题。
        kind = "用例失败" if result.returncode == 1 else f"pytest 未能正常执行（rc={result.returncode}）"
        print(f"ASSERTION SYNC: --run 结果 {'FAIL' if run_failed else 'PASS'}（{kind}）")

    if run_failed:
        return 1
    if not hints:
        print('ASSERTION SYNC: PASS（没有「改了函数体但覆盖用例未动」的情形）')
        return 0
    print(f"ASSERTION SYNC: {len(hints)} 条提示（提示不是缺陷，请人工确认旧断言是否仍成立）")
    for line in hints:
        print(f"  - {line}")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
