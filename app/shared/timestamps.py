"""UTC 时间戳的对外格式。

模型列声明为 `DateTime(timezone=True)`，但 SQLite 读回来是 naive、PostgreSQL 是
tz-aware，同一端点在两种方言下会给出 `...T07:20:00.123456` 与 `...+00:00` 两种形状；
带 `+00:00` 的值再被塞回查询串时，`+` 会按规范解成空格（阶段 G1 的审计游标 422 即如此）。
统一成 `...Z`：两方言一致，且能原样回塞。
"""

from __future__ import annotations

from datetime import datetime, timezone


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
