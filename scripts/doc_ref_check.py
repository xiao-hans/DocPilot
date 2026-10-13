# -*- coding: utf-8 -*-
"""文档交叉引用与用例编号核对（维护轮 M19）。

## 为什么要有这个脚本

M19 复核时人工扫了一遍 `docs/` 的 `docs/NN §X` 引用，查出 13 处腐烂：目标节不存在、
编号写成行号、引用叙述本身含字面编号。这些全是多轮人工写作累积出来的，**不自动化就会
重新长出来**。本轮一次性核对器放在 `outputs/`（被 gitignore），目录一清就没了，所以固化进来。

## 用法

    .venv/Scripts/python.exe scripts/doc_ref_check.py

退出码 0 表示全部通过；非 0 表示有待修的条目，逐条打印文件名与行号。

## 三条自我约束（都是本轮踩出来的）

1. **文件名在脚本内枚举，不从命令行传参**。中文路径经 PowerShell 传给子进程会串码，
   上一版因此"读不到文件"，却把它读成了"零命中"——一个正好符合预期的假答案。
   现在输出行数为 0 会显式告警。
2. **不按行号比较命中**。往文档里插几十行会让 HEAD 的行号整体位移，按行号比较会把
   "既有命中挪了位置"误判成"新增命中"（维护轮 M9 犯过这个错）。
3. **判据要能被反向验证**。注入一个超范围的 `§10.99` 与一个不存在的 `§99.1`，脚本必须报；
   同时 `docs/11 §10.4`（第 10 节的第 4 个编号条目，真实存在）必须不报。
   只认标题的核对器会把后一种正常简写报成断链，从而诱使后人去改动本来正常的行文。
"""

from __future__ import annotations

import pathlib
import re
import sys

HEADING = re.compile(r"^#{2,6}\s+([0-9]+(?:\.[0-9]+)*)\b", re.M)
CROSS_REF = re.compile(r"docs/(\d{2})[^\s`|，。;；)]*\s*§\s*([0-9]+(?:\.[0-9]+)*)")
BARE_REF = re.compile(r"§\s*([0-9]+(?:\.[0-9]+)*)")
TC_ID = re.compile(r"TC-[A-Z]+-\d{3}[a-z]?")
CASES = "06-测试计划与测试用例.md"
MATRIX = "07-需求追踪矩阵.md"


def load_docs(root: pathlib.Path) -> dict[str, str]:
    """枚举 docs 下全部 Markdown。

    返回空集合是危险信号而不是"干净"，所以调用方要检查数量。
    """
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(root.rglob("*.md"))}


def section_ids(text: str) -> set[str]:
    """该文档里出现过的节号，含父级（写了 5.3 就算 5 也存在）。"""
    out: set[str] = set()
    for m in HEADING.finditer(text):
        parts = m.group(1).split(".")
        for i in range(1, len(parts) + 1):
            out.add(".".join(parts[:i]))
    return out


def numbered_items(text: str, sec: str) -> int:
    """标题号恰为 sec 的那一节里，顶层编号条目（`1.` / `2.` …）的个数。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = re.match(r"^#{2,6}\s+([0-9]+(?:\.[0-9]+)*)\b", line)
        if m and m.group(1) == sec:
            start = i
            break
    if start is None:
        return 0
    count = 0
    for line in lines[start + 1:]:
        if re.match(r"^#{2,6}\s+[0-9]", line):
            break
        if re.match(r"^\d+\.\s", line):
            count += 1
    return count


def resolvable(sec: str, text: str) -> bool:
    """编号能否落到真实位置：小节标题，或"第 N 节的第 k 个编号条目"。"""
    if re.search(r"^#{2,6}\s+" + re.escape(sec) + r"\b", text, re.M):
        return True
    parent, _, tail = sec.rpartition(".")
    return bool(parent and tail.isdigit()) and numbered_items(text, parent) >= int(tail)


def line_of(text: str, needle_re: re.Pattern[str]) -> list[int]:
    """在**原文**里定位所有命中行。用抹掉前缀的副本去数换行会整体偏移。"""
    return [i + 1 for i, ln in enumerate(text.splitlines()) if needle_re.search(ln)]


def check(docs: dict[str, str]) -> list[str]:
    defined = {name: section_ids(text) for name, text in docs.items()}
    problems: list[str] = []

    # 1) 跨文档引用：`docs/NN … §X`
    for name, text in docs.items():
        for m in CROSS_REF.finditer(text):
            num, sec = m.group(1), m.group(2)
            target = next((k for k in docs if k.startswith(num + "-")), None)
            if target is None:
                problems.append(f"{name}: 引用了不存在的 docs/{num}")
                continue
            if not resolvable(sec, docs[target]):
                line = text[: m.start()].count("\n") + 1
                problems.append(f"{name}:L{line}: docs/{num} §{sec} 无此节")

    # 2) 裸 § 引用在整个文档集里都无法解析
    all_secs = set().union(*defined.values()) if defined else set()
    for name, text in docs.items():
        stripped = CROSS_REF.sub(" ", text)  # 跨文档的已在上面判定过
        for m in BARE_REF.finditer(stripped):
            sec = m.group(1)
            if sec in all_secs or sec in defined[name]:
                continue
            if any(resolvable(sec, other) for other in docs.values()):
                continue
            lines = line_of(text, re.compile(r"§" + re.escape(sec) + r"(?![0-9.])"))
            problems.append(f"{name}:L{','.join(map(str, lines)) or '?'}: §{sec} 全文档集无此节")

    # 3) 用例编号双向闭合
    if CASES in docs and MATRIX in docs:
        cases = set(TC_ID.findall(docs[CASES]))
        matrix = set(TC_ID.findall(docs[MATRIX]))
        for tc in sorted(cases - matrix):
            problems.append(f"{MATRIX}: 缺 {tc}（{CASES} 里有）")
        for tc in sorted(matrix - cases):
            if re.sub(r"[a-z]$", "", tc) not in cases:
                problems.append(f"{CASES}: 缺 {tc}（{MATRIX} 里有）")
    return problems


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent / "docs"
    docs = load_docs(root)
    if len(docs) < 20:
        # 读到空或半数是环境失败，不是"文档干净"。宁可报错也不要给假零。
        print(f"读到 {len(docs)} 个文档，疑为路径或编码问题，读数不可信。", file=sys.stderr)
        return 2
    problems = check(docs)
    for line in problems:
        print("FAIL", line)
    print(f"DOC REF CHECK: {'PASS' if not problems else 'FAIL'}（文档 {len(docs)} 个，问题 {len(problems)} 条）")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
