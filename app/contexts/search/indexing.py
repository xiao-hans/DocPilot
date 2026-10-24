"""检索索引的来源规则：一次重建要把哪些内容写进索引（`docs/15 §5`）。

这个模块存在的唯一理由是**打断一个真实的模块环**：`service.py` 需要
`backend.get_search_backend()`，而 `backend.SqlSearchBackend.reindex()` 又需要
「来源有哪些」这条规则。把规则放在 `service.py` 里，两文件就互为依赖；
放在这里，`service` 与 `backend` 都只单向依赖本模块。

规则本身不能复制两份：复制会让"来源有哪些"出现两个权威，而它是 `docs/15 §5`
定下的语义（文件名 / 原文 / 字段值三类同事务重建）。
"""

from __future__ import annotations

from typing import Any

from app.contexts.search.domain import DEFAULT_FIELD_NAME
from app.shared.text_safety import without_nul

MAX_FIELD_NAME_CHARS = 120


def _content_of(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    # 列表/字典按稳定顺序展开：检索读模型不允许出现"无法比较"的原始对象。
    return " ".join(_content_of(item) for item in _flatten(value))


def _flatten(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [value[key] for key in sorted(value)]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def build_wanted_entries(
    *, filename: str, text: str, values: dict[str, Any]
) -> dict[tuple[str, str], str]:
    """把一次重建的"来源"整理成 `(kind, field_name) → content`。"""
    wanted: dict[tuple[str, str], str] = {
        ("filename", DEFAULT_FIELD_NAME): filename,
        ("text", DEFAULT_FIELD_NAME): text,
    }
    for name, value in values.items():
        wanted[("field", str(name)[:MAX_FIELD_NAME_CHARS])] = _content_of(value)
    # PostgreSQL 的 text 与 json 都拒绝 NUL 字节，而 SQLite 照收；索引内容必须在入库前清掉，
    # 否则一条脏文本会让整批写入回滚。
    return {key: without_nul(content) for key, content in wanted.items()}
