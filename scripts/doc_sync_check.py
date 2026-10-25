"""文档与代码的同步核对（维护轮 M33）。

## 要有这个脚本

`scripts/doc_ref_check.py` 管的是**文档内部的引用**（`docs/NN §X` 是否存在、TC 编号是否双向闭合）。
它管不到另一类腐烂：**文档里写着的代码事实，已经和仓库不一致了**。

M32 收尾时人工核了一遍，查出两处会实际误导人的残留——README 的复制块漏了 `and not clamd`，
`docs/09 §268` 还写着"测试因此分三层"。同一轮里 `docs/24` 与 `docs/06` 都写对了，**只有没人碰的那段是错的**。
这正是"我改过哪里"不是"哪里需要改"的又一次实例（M21、M32 各记过一次）。
不自动化就会重新长出来，所以固化成脚本。

## 三条判据

- **A 代码路径**：活文档里被反引号引用的 `app/…`、`tests/…`、`scripts/…`、`frontend/…` 必须能落到仓库里。
  认三种写法：花括号展开（`app/contexts/review/{domain,repository}.py`）、成员引用
  （`tests/test_x.py::test_y`，只核对 `::` 之前那一段——函数名由判据 B 负责）、
  以及属性引用（`app/db.assert_disposable_database()`，剥掉尾部属性后落到 `app/db.py`）。
  含 `*` 或 `…` 的通配写法不判——它表达的是"这一类"，没有唯一落点。
- **B 用例函数名**：活文档里被反引号引用、且**不是文件路径**的 `test_xxx`，必须在 `tests/` 里真有
  `def test_xxx`。被改名或被删掉的用例名，读起来和真用例一模一样，这是最容易骗过读者的一种腐烂。
  `tests/test_membership.py` 里的 `test_membership` 是**文件名**，归判据 A，判据 B 必须让开——
  M33 第一版正是在这里把 149 条假红喷了出来（见 `docs/09 §72.4`）。
- **C 门禁命令与标记对齐**：`pytest.ini` 注册的每个 marker，必须在 README 的「测试与门禁」代码块里看得见
  ——要么有 `-m <name>` 的独立命令，要么出现在默认套件的 `not <name>` 里。
  加了新 marker 却只改了某个 docs 小节，照 README 复制命令的人就会把集成用例拉进基础套件。

## 为什么 `docs/09` 不参与 A、B

`docs/09` 是编年史：它逐轮记录"当时改了什么、当时叫什么名字"。里面出现 `app/services/pipeline.py`
是**历史事实**——那时候文件就在那里。要求编年史里的路径今天仍然存在，等于要求历史随时间改写。
所以 A、B 跳过它，但在输出里显式列出"跳过了谁、为什么"，不静默略过。
判据 C 不涉及编年史，对 README 生效。

排除清单在脚本内枚举，不从命令行传参——理由与 `doc_ref_check.py` 同：中文路径经 PowerShell
传给子进程会串码，上一版因此"读不到文件"，却把它读成了"零命中"，一个正好符合预期的假答案。

## 用法

    .venv/Scripts/python.exe scripts/doc_sync_check.py

退出码 0 表示全部通过；非 0 表示有待修的条目，逐条打印文件名与行号。

## 反向验证（判据自己也得被验）

注入三种退化，脚本必须各自报红，还原后必须回绿：

1. 在 `README.md` 写一个不存在的 `app/nowhere/gone.py` → A 报；
2. 在 `docs/06` 写一个不存在的 `test_this_case_never_existed` → B 报；
3. 从 `README.md` 删掉 `-m clamd` 那一行（保留 `not clamd`）→ 不报（默认行仍覆盖它）；
   再把 `not clamd` 也删掉 → C 报。

第 3 条必须分成两步：只要还有一处提到，就不该报——否则脚本会逼着人到处重复同一句话。
"""

from __future__ import annotations

import io
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# A/B 只看"活文档"：它们描述现在。编年史只对判据 C 生效。
SKIP_AB = {"09-工程实施记录.md"}

# 只认这些顶层目录，避免把 `docs/22-…` 之类的文档路径当成代码路径来判。
CODE_ROOTS = ("app/", "tests/", "scripts/", "frontend/")

BACKTICK = re.compile(r"`([^`\n]+)`")
PATH_TOKEN = re.compile(
    r"(?:" + "|".join(re.escape(r) for r in CODE_ROOTS) + r")[\w./{},-]*"
)
TEST_NAME = re.compile(r"(?<![\w.])test_[A-Za-z0-9_]+")
BRACE = re.compile(r"\{([^{}]*)\}")

# 历史标记词。一行里出现这些词，就表示它在讲“过去叫什么 / 过去在哪”，
# 这时里面那个名字今天不存在是**正确的**，不算腐烂。
# 这条约定把“我在引用现在”与“我在追述历史”分开——否则脚本会逼着人
# 删掉全部可追溯的更名记录（M33 第一版就是两边都错：先是 149 条假红，
# 再把历史记录也当成错）。
HISTORY = re.compile(r"更名|原名|旧名|曾用名|搬迁前|迁入|立项时|当时|历史|此前|已删|已不存在")


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def live_docs() -> dict[str, str]:
    """活文档 = `docs/` 下除编年史以外的全部 Markdown，**加上 README**。

    README 一度没被扫到：M33 第一版只枚举 `docs/*.md`，于是反向验证里
    “往 README 写一个不存在的路径”那一面是**假绿**（注入了不报）。
    最常被读的那份文档反而没人管，这个漏洞由反向验证自己照出来。
    """
    docs = {p.name: read(p) for p in sorted((ROOT / "docs").glob("*.md"))}
    readme = ROOT / "README.md"
    if readme.exists():
        docs["README.md"] = read(readme)
    return docs


def expand_braces(token: str) -> list[str]:
    """`a/{b,c}.py` → `a/b.py`、`a/c.py`。只展开一层，够用且不会爆炸。"""
    m = BRACE.search(token)
    if not m:
        return [token]
    out: list[str] = []
    head, tail = token[: m.start()], token[m.end():]
    for part in m.group(1).split(","):
        out.extend(expand_braces(head + part.strip() + tail))
    return out


def resolves(token: str) -> bool:
    """一个路径写法能否落到仓库里。

    依次试：原样（文件或目录）、补 `.py`、补 `/`，再逐段剥掉尾部属性后重试
    （`app/db.assert_disposable_database()` → `app/db.assert_disposable_database` → `app/db`）。
    """
    t = token.rstrip(".,;:)]")
    if t.endswith("()"):
        t = t[:-2]
    candidates = [t]
    while "." in t.rsplit("/", 1)[-1]:
        t = t.rpartition(".")[0]
        candidates.append(t)
    for c in candidates:
        if not c:
            continue
        if (ROOT / c).exists() or (ROOT / (c + ".py")).exists() or (ROOT / (c + "/")).exists():
            return True
    return False


def test_defs() -> set[str]:
    """`tests/` 下真实存在的测试函数名。"""
    names: set[str] = set()
    for p in (ROOT / "tests").rglob("*.py"):
        names |= set(
            re.findall(r"^\s*(?:async\s+)?def (test_[A-Za-z0-9_]+)", read(p), re.M)
        )
    return names


def test_files() -> set[str]:
    """`tests/` 下的文件名（不含扩展名）。判据 B 用它让开对文件名的引用。"""
    return {p.name[: -len(".py")] for p in (ROOT / "tests").rglob("*.py")}


def marker_names() -> list[str]:
    """`pytest.ini` 里注册的 marker 名。"""
    parts = read(ROOT / "pytest.ini").split("markers =", 1)
    if len(parts) < 2:
        return []
    return re.findall(r"^\s{4}([a-z][a-z0-9_]*):", parts[1], re.M)


def readme_gate_block() -> str:
    """README 里「测试与门禁」之后的第一段 ```bash 代码块。"""
    text = read(ROOT / "README.md")
    start = text.find("## 测试与门禁")
    if start < 0:
        return ""
    fence = text.find("```bash", start)
    if fence < 0:
        return ""
    end = text.find("```", fence + len("```bash"))
    return text[fence + len("```bash") : (end if end > 0 else len(text))]


def check_paths(docs: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for name, text in docs.items():
        if name in SKIP_AB:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if HISTORY.search(line):
                continue  # 这一行在追述过去，不要求名字今天仍成立
            for span in BACKTICK.findall(line):
                for token in PATH_TOKEN.findall(span):
                    if "*" in token or "…" in token:
                        continue  # 「这一类」，没有唯一落点
                    for candidate in expand_braces(token):
                        if not resolves(candidate):
                            problems.append(
                                f"{name}:L{lineno}: 引用了不存在的路径 {candidate}"
                            )
    return problems


def check_test_names(docs: dict[str, str]) -> list[str]:
    defined = test_defs()
    if not defined:
        # 读到空是环境失败，不是"文档干净"。宁可报错也不要给假零。
        raise SystemExit("tests/ 下没抽到任何 test_ 定义，读数不可信。")
    file_stems = test_files()
    problems: list[str] = []
    for name, text in docs.items():
        if name in SKIP_AB:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if HISTORY.search(line):
                continue  # 这一行在追述过去，不要求名字今天仍成立
            for span in BACKTICK.findall(line):
                s = span.strip()
                if "*" in s or "…" in s:
                    continue  # 通配写法，表达的是“这一类”
                if "::" in s:
                    # 成员引用：`path::name`，只核对 name 那一段。
                    s = s.split("::", 1)[1]
                elif "/" in s or s.endswith(".py"):
                    continue  # 文件名，归判据 A
                for token in TEST_NAME.findall(s):
                    if token in defined or token in file_stems:
                        continue
                    problems.append(
                        f"{name}:L{lineno}: 引用了不存在的用例 {token}（原文 `{span}`）"
                    )
    return problems


def check_markers() -> list[str]:
    markers = marker_names()
    if not markers:
        raise SystemExit("pytest.ini 里没抽到任何 marker，读数不可信。")
    block = readme_gate_block()
    if not block:
        raise SystemExit("README 里没找到「测试与门禁」代码块，读数不可信。")
    problems: list[str] = []
    for m in markers:
        # 独立命令 `-m clamd`，或默认行里的 `not clamd`，任一处即可。
        if re.search(r"-m\s+[\"']?[^\n\"']*\b" + re.escape(m) + r"\b", block):
            continue
        problems.append(
            f"README.md: 门禁块里没有 marker {m!r}"
            f"（既无 -m {m}，也不在默认套件的排除列表里）"
        )
    return problems


def main() -> int:
    docs = live_docs()
    if len(docs) < 20:
        print(f"读到 {len(docs)} 个文档，疑为路径或编码问题，读数不可信。", file=sys.stderr)
        return 2

    problems = check_paths(docs) + check_test_names(docs) + check_markers()
    for line in problems:
        print("FAIL", line)

    skipped = "、".join(sorted(SKIP_AB))
    print(
        f"DOC SYNC CHECK: {'PASS' if not problems else 'FAIL'}"
        f"（文档 {len(docs)} 个，问题 {len(problems)} 条；"
        f"A/B跳过编年史 {skipped}——它记录当时的名字，不要求今天仍成立）"
    )
    return 0 if not problems else 1


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
