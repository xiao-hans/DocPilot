"""检索用例：与源数据同事务重建索引，查询只在本租户范围内命中（DOC-DD-006 §6.1/§6.2）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contexts.search import repository
from app.contexts.search.backend import get_search_backend
from app.contexts.search.domain import DEFAULT_FIELD_NAME, SEARCH_KINDS, SearchEntry
from app.errors import AccessError
from app.services.text_safety import without_nul

MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 60
SNIPPET_RADIUS = 40
PAGE_MAX = 100
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
    """把一次重建的"来源"整理成 `(kind, field_name) → content`。

    抽出来是为了让默认后端（`backend.SqlSearchBackend`）与 service 共用同一份来源规则：
    复制一份会让"来源有哪些"这件事出现两个权威，而它是 `docs/15 §5` 定下的语义
    （文件名/原文/字段值三类同事务重建）。
    """
    wanted: dict[tuple[str, str], str] = {
        ("filename", DEFAULT_FIELD_NAME): filename,
        ("text", DEFAULT_FIELD_NAME): text,
    }
    for name, value in values.items():
        wanted[("field", str(name)[:MAX_FIELD_NAME_CHARS])] = _content_of(value)
    # PostgreSQL 的 text 与 json 都拒绝 NUL 字节，而 SQLite 照收；索引内容必须在入库前清掉，
    # 否则一条脏文本会让整批写入回滚。
    return {key: without_nul(content) for key, content in wanted.items()}


def reindex_version(
    db: Session,
    *,
    tenant_id: str,
    document_id: str,
    document_version_id: str,
    filename: str,
    text: str,
    values: dict[str, Any],
    now: datetime,
) -> None:
    """重建一个文件版本的索引：同来源覆盖、缺失来源清除，行数不随重跑增长。

    经后端契约转发（`docs/22`），因此换成别的检索后端时调用方不必改。
    """
    get_search_backend().reindex(
        db,
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=document_version_id,
        filename=filename,
        text=text,
        values=values,
        now=now,
    )


def _like_pattern(term: str) -> str:
    """转义 LIKE 通配符：用户输入里的 % 和 _ 必须按字面匹配，否则会命中全表。"""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(content: str, term: str) -> str:
    lowered, needle = content.lower(), term.lower()
    index = lowered.find(needle)
    if index < 0:
        index = 0
    start = max(0, index - SNIPPET_RADIUS)
    end = min(len(content), index + len(needle) + SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{content[start:end].replace(chr(10), ' ')}{suffix}"


def search(
    db: Session,
    *,
    tenant_id: str,
    q: str,
    kind: str | None = None,
    doc_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """按关键词检索文件名、解析原文与已提取字段值。"""
    term = (q or "").strip()
    if not MIN_QUERY_CHARS <= len(term) <= MAX_QUERY_CHARS:
        raise AccessError(
            422, f"查询词去除首尾空白后长度需为 {MIN_QUERY_CHARS}-{MAX_QUERY_CHARS} 个字符"
        )
    if kind is not None and kind != "all" and kind not in SEARCH_KINDS:
        raise AccessError(422, "kind 非法")

    total, rows = get_search_backend().query(
        db,
        tenant_id=tenant_id,
        pattern=_like_pattern(term.lower()),
        kind=kind if kind in SEARCH_KINDS else None,
        doc_type=doc_type,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "hits": [
            {
                "document_id": entry.document_id,
                "document_version_id": entry.document_version_id,
                "kind": entry.kind,
                "field_name": entry.field_name or None,
                "doc_type": row_doc_type,
                "snippet": _snippet(entry.content, term),
            }
            for entry, row_doc_type in rows
        ],
    }
