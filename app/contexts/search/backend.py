"""检索后端的可替换契约（NFR-004，`docs/22`）。

## 为什么有这一层

`NFR-004` 要求"文档解析器、提取器、搜索后端和集成适配器应通过接口替换/扩展"。
前三类在维护轮 M2 的契约测试里已确认有接缝，**只有搜索后端没有**——检索直接查
`search_entries` 表，换后端要改 service 与 repository 两层，也没有可注入的接口，
因此"换后端"这件事无法在不改产品代码的前提下被验证。

## 两侧必须成对替换

`reindex`（写索引）与 `query`（查索引）放在**同一个** ABC 里，不是把两个无关函数凑一起：
只换查询而保留写侧，会让索引行与查询语义错位——例如换成词元匹配的查询、索引仍按原文
整段存，结果永远是零命中，而两侧各自的用例都可能是绿的。

## 契约收的是"已转义的 pattern"，不是原始查询词

转义（`_like_pattern`）与限长校验留在 service。理由是转义属于**查询语义**：
换成词元匹配的后端时 `%` 不再是通配符，转义规则完全不同。把它放进契约会让每个后端
各写一份转义，而 service 就失去了"查询词合法性"这一层的唯一权威。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app import config
from app.contexts.search import repository
from app.contexts.search.domain import SearchEntry


class SearchBackend(ABC):
    """检索后端的统一接口：重建索引与查询各一组。"""

    @abstractmethod
    def reindex(
        self,
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
        """把一个文件版本的索引重建为给定来源；同来源覆盖、缺失来源清除。

        实现方决定"来源"如何落成索引行（当前实现是 filename/text/field 三类）。
        """

    @abstractmethod
    def query(
        self,
        db: Session,
        *,
        tenant_id: str,
        pattern: str,
        kind: str | None,
        doc_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[tuple[SearchEntry, str | None]]]:
        """按**已转义**的模式查询，返回 `(总数, [(索引条目, 文档类型)])`。"""


class SqlSearchBackend(SearchBackend):
    """默认实现：`search_entries` 表 + `lower(content) LIKE`。

    行为与抽契约之前**逐字一致**：两个方法分别转发到 `repository` 的四个函数，
    转义与限长仍由 `service` 负责，这里只收已转义的 pattern。
    `escape="\\\\"` 是 `LIKE` 专用语法，留在这一层内部——契约不该逼别的后端假装自己是 SQL。
    """

    def reindex(
        self,
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
        from app.contexts.search.service import build_wanted_entries

        wanted = build_wanted_entries(filename=filename, text=text, values=values)
        existing = {
            (entry.kind, entry.field_name): entry
            for entry in repository.entries_for_version(db, tenant_id, document_version_id)
        }
        for (kind, field_name), content in wanted.items():
            entry = existing.get((kind, field_name))
            if entry is None:
                db.add(
                    SearchEntry(
                        id=uuid4().hex,
                        tenant_id=tenant_id,
                        document_id=document_id,
                        document_version_id=document_version_id,
                        kind=kind,
                        field_name=field_name,
                        content=content,
                        updated_at=now,
                    )
                )
            else:
                entry.content = content
                entry.updated_at = now
        for (kind, field_name), entry in existing.items():
            if (kind, field_name) not in wanted:
                db.delete(entry)

    def query(
        self,
        db: Session,
        *,
        tenant_id: str,
        pattern: str,
        kind: str | None,
        doc_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[tuple[SearchEntry, str | None]]]:
        return repository.search_entries(
            db,
            tenant_id=tenant_id,
            pattern=pattern,
            kind=kind,
            doc_type=doc_type,
            limit=limit,
            offset=offset,
        )


_backend: SearchBackend | None = None
_backend_name: str | None = None


def get_search_backend() -> SearchBackend:
    """按 `SEARCH_BACKEND` 选实现；默认 `sql`。

    未知值**显式抛错**，与 `STORAGE_BACKEND`、`EXTRACTOR_MODE` 同一套纪律：配置写错了要立刻失败，
    不能静默退回默认实现——那会让人以为换后端生效了，实际还在查原来的表。

    与流水线的工厂不同，这里**每次调用都读配置**（缓存按名字失效）：
    `docs/06 §5.25` 记过一个教训——构造时绑定会让"换工厂"这件事在替换点失效，
    而检索是短路径、没有性能理由去提前绑定。
    """
    global _backend, _backend_name
    name = config.SEARCH_BACKEND
    if _backend is not None and _backend_name == name:
        return _backend
    if name == "sql":
        backend: SearchBackend = SqlSearchBackend()
    else:
        raise RuntimeError(f"未知的 SEARCH_BACKEND：{name!r}（当前可选 sql）")
    _backend, _backend_name = backend, name
    return backend
