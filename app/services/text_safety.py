"""入库前必须去掉的字符。

PostgreSQL 的 text 与 json 一律拒绝 NUL（0x00），SQLite 照收：一条脏 OCR 文本在
本机什么都不会发生，到 PostgreSQL 上则让整个写入事务回滚。因此凡是承载外部文本的
入库边界都要先过这里，而不是在每个调用点各写一遍 replace。
"""

from __future__ import annotations

from typing import Any


def without_nul(value: str) -> str:
    return value.replace("\x00", "")


def clean_payload(value: Any) -> Any:
    """递归剔除字符串里的 NUL，保持标量类型不变。

    数字清洗后仍是数字（提取值类型会影响复核与导出），只有 `str` 会被改写；
    字典的键同样要清洗——json 列里键与值是同一份序列化文本。
    """
    if isinstance(value, str):
        return without_nul(value)
    if isinstance(value, dict):
        return {
            (without_nul(key) if isinstance(key, str) else key): clean_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [clean_payload(item) for item in value]
    return value
