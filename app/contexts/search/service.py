"""检索用例：与源数据同事务重建索引，查询只在本租户范围内命中（DOC-DD-006 §6.1/§6.2）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contexts.search import repository
from app.contexts.search.backend import get_search_backend
from app.contexts.search.domain import SEARCH_KINDS, SearchEntry
from app.contexts.search.indexing import build_wanted_entries
from app.platform.errors import AccessError

MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 60
SNIPPET_RADIUS = 40
PAGE_MAX = 100

__all__ = [
    "MIN_QUERY_CHARS",
    "MAX_QUERY_CHARS",
    "PAGE_MAX",
    "build_wanted_entries",
    "reindex_version",
    "search",
]


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
