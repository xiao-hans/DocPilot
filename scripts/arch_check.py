"""代码结构检查：分层依赖规则与模块环。

只做静态分析（AST），不导入被测模块，不改任何文件。

用法：
    python scripts/arch_check.py                 # 打印违规，退出码 1 表示有违规
    python scripts/arch_check.py --write-baseline # 用当前违规覆盖基线文件
    python scripts/arch_check.py --quiet         # 只打印计数

规则编号见 docs/12 §7.1。基线文件记录重构起点，检查只报告"基线之外的新违规"，
所以重构过程中可以先清零再回写基线。
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"
BASELINE = ROOT / "tests" / "architecture_baseline.json"

APP_ROOT_MODULES = {
    "app.platform.config",
    "app.platform.db",
    "app.platform.errors",
    "app.platform.security",
    "app.platform.request_context",
}

# 进程装配根：这四个入口负责把具体实现接到一起，依赖上下文是它们的职责而不是越界。
# 它们也是 Dockerfile 与 docker-compose.yml 里声明的四个进程入口。
COMPOSITION_ROOTS = {
    "app.main",  # uvicorn app.main:app
    "app.dispatcher",  # python -m app.dispatcher
    "app.worker",  # python -m app.worker
    "app.webhooks",  # python -m app.webhooks
}


def module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(path: pathlib.Path) -> tuple[set[str], set[str]]:
    """返回 (app 内导入, 第三方/标准库顶层名)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    app_deps: set[str] = set()
    foreign: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            target = node.module or ""
            if target == "app" or target.startswith("app."):
                app_deps.add(target)
            elif target:
                foreign.add(target.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "app" or name.startswith("app."):
                    app_deps.add(name)
                else:
                    foreign.add(name.split(".")[0])
    return app_deps, foreign


def is_pkg_init(path: pathlib.Path) -> bool:
    return path.name == "__init__.py"


def scan() -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, pathlib.Path]]:
    edges: dict[str, set[str]] = {}
    foreign: dict[str, set[str]] = {}
    paths: dict[str, pathlib.Path] = {}
    for path in sorted(APP.rglob("*.py")):
        name = module_name(path)
        deps, fgn = imports_of(path)
        edges[name] = deps
        foreign[name] = fgn
        paths[name] = path
    return edges, foreign, paths


def violations(edges: dict[str, set[str]], foreign: dict[str, set[str]]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def add(rule: str, src: str, dep: str, why: str) -> None:
        found.append({"rule": rule, "src": src, "dep": dep, "why": why})

    for src, deps in sorted(edges.items()):
        for dep in sorted(deps):
            # R1 shared 不得依赖业务或适配器
            if src.startswith("app.shared.") and (
                dep.startswith("app.contexts.") or dep.startswith("app.adapters.")
            ):
                add("R1", src, dep, "shared 层不得依赖上下文或适配器")
            # R2 adapters 不得依赖业务
            if src.startswith("app.adapters.") and dep.startswith("app.contexts."):
                add("R2", src, dep, "适配器不得依赖上下文")
            # R4 platform 不得依赖业务或适配器
            if src.startswith("app.platform.") and (
                dep.startswith("app.contexts.") or dep.startswith("app.adapters.")
            ):
                add("R4", src, dep, "platform 层不得依赖上下文或适配器")
            # R5 models 不得依赖上下文
            if src.startswith("app.models.") and dep.startswith("app.contexts."):
                add("R5", src, dep, "models 不得反向依赖上下文")
            # R6 domain 不得依赖传输层（本上下文 api 或 platform.http）
            if src.startswith("app.contexts.") and src.endswith(".domain") and (
                dep.startswith("app.platform.http")
                or (dep.startswith("app.contexts.") and ".api" in dep)
            ):
                add("R6", src, dep, "domain 不得依赖传输层")
            # R7 只有装配根可以依赖全部
            if (
                src not in COMPOSITION_ROOTS
                and not src.startswith(("app.runtime.", "app.contexts.", "app.adapters."))
                and dep.startswith("app.contexts.")
                and src not in APP_ROOT_MODULES
            ):
                add("R7", src, dep, "非装配点不得直接依赖上下文")

    # R3 domain 层不得导入传输/持久化设施
    for src, fgn in sorted(foreign.items()):
        if not src.startswith("app.contexts.") or not src.endswith(".domain"):
            continue
        for bad in ("fastapi", "starlette", "uvicorn", "requests", "httpx"):
            if bad in fgn:
                add("R3", src, bad, "domain 不得导入传输层设施")

    return found


def cycles(edges: dict[str, set[str]], paths: dict[str, pathlib.Path]) -> list[list[str]]:
    """模块级环检测；忽略 __init__ 节点（包导入不等于循环依赖）。"""
    nodes = {m for m in edges if not is_pkg_init(paths[m])}
    graph = {
        m: {d for d in edges[m] if d in nodes and not is_pkg_init(paths[d])}
        for m in nodes
    }
    found: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    color = {m: 0 for m in nodes}
    stack: list[str] = []
    sys.setrecursionlimit(20000)

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for dep in sorted(graph[node]):
            if color[dep] == 1:
                i = stack.index(dep)
                cyc = stack[i:]
                key = frozenset(cyc)
                if key not in seen:
                    seen.add(key)
                    found.append(cyc + [dep])
            elif color[dep] == 0:
                dfs(dep)
        stack.pop()
        color[node] = 2

    for m in sorted(nodes):
        if color[m] == 0:
            dfs(m)
    return found


def key_of(item: dict[str, str]) -> str:
    return f"{item['rule']}|{item['src']}|{item['dep']}"


def cycle_key(cyc: list[str]) -> str:
    """环按成员集合归一，不依赖起点，也不依赖 DFS 顺序。"""
    return "cycle|" + "|".join(sorted(set(cyc)))


def load_baseline() -> tuple[set[str], set[str]]:
    if not BASELINE.exists():
        return set(), set()
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    vios = {key_of(i) for i in data.get("violations", [])}
    cycs = {cycle_key(c) for c in data.get("cycles", [])}
    return vios, cycs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    edges, foreign, paths = scan()
    found = violations(edges, foreign)
    cyc = cycles(edges, paths)

    if args.write_baseline:
        payload = {
            "note": "重构起点快照。检查只报告基线之外的新违规。",
            "violations": found,
            "cycles": cyc,
        }
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"基线已写入 {BASELINE.relative_to(ROOT)}：违规 {len(found)} 条、环 {len(cyc)} 个")
        return 0

    baseline_vios, baseline_cycs = load_baseline()
    current_vio_keys = {key_of(i) for i in found}
    current_cyc_keys = {cycle_key(c) for c in cyc}
    new = [i for i in found if key_of(i) not in baseline_vios]
    fixed = baseline_vios - current_vio_keys
    new_cyc = [c for c in cyc if cycle_key(c) not in baseline_cycs]
    fixed_cyc = baseline_cycs - current_cyc_keys

    if args.quiet:
        print(
            f"ARCH CHECK: 违规 {len(found)}（基线外新增 {len(new)}，已消除 {len(fixed)}），"
            f"环 {len(cyc)}（基线外新增 {len(new_cyc)}，已消除 {len(fixed_cyc)}）"
        )
        return 1 if (new or new_cyc) else 0

    print(f"模块总数 {len(edges)}；违规合计 {len(found)}；基线外新增 {len(new)}；已消除 {len(fixed)}")
    if new:
        print("\n=== 基线外新增违规")
        for i in new:
            print(f"  [{i['rule']}] {i['src']} -> {i['dep']}  ({i['why']})")
    if fixed:
        print("\n=== 本轮已消除违规（应回写基线）")
        for k in sorted(fixed):
            print(f"  {k}")
    if new_cyc:
        print("\n=== 基线外新增环")
        for c in new_cyc:
            print("  " + " -> ".join(c))
    if fixed_cyc:
        print("\n=== 本轮已消除环（应回写基线）")
        for k in sorted(fixed_cyc):
            print(f"  {k}")
    if not new and not new_cyc:
        print(f"\nARCH CHECK: PASS（基线内保留 {len(found)} 条违规、{len(cyc)} 个环）")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
