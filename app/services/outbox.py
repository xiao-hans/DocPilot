"""领域事件写入：与业务变更同事务落库，投递策略在后续阶段接入。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy.orm import Session

from app.models.outbox import DomainEventRecord


def _reject_absolute_paths(value: Any, *, where: str) -> None:
    """递归检查载荷：绝对路径可能藏在嵌套 dict/list 里，也可能以 UNC 形式出现。"""
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_absolute_path_value(key, item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_absolute_path_value(index, item, f"{where}[{index}]")
    elif isinstance(value, str) and _looks_like_absolute_path(value):
        raise ValueError(f"事件载荷不得包含绝对路径: {where}")


def _reject_absolute_path_value(key: Any, value: Any, where: str) -> None:
    if isinstance(value, str) and _looks_like_absolute_path(value):
        raise ValueError(f"事件载荷不得包含绝对路径: {where}")
    _reject_absolute_paths(value, where=where)


def _looks_like_absolute_path(value: str) -> bool:
    if not value:
        return False
    return (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or value.startswith("\\\\")
    )


def record(
    db: Session,
    *,
    tenant_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Mapping[str, Any] | None = None,
) -> DomainEventRecord:
    """追加事件；载荷只允许 ID 与事实，绝对路径直接拒绝。"""
    clean = dict(payload or {})
    _reject_absolute_paths(clean, where="payload")
    event = DomainEventRecord(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=clean,
        occurred_at=datetime.now(timezone.utc),
    )
    db.add(event)
    return event
