"""跨租户泄漏探针的判定部分（`scripts/capacity_observation.py` 用它扫响应体）。

单独成模块是为了让这条判据能被用例直接证伪。它原先是"任何 9 开头 20 位数字都算标记"，
G28 复跑观测时因此报了 3 处"未知标记"泄漏——查下来是 SHA-256 校验和里的一段 20 位数字，
跟租户边界毫无关系。**判据过宽会把噪声读成越权**，而收紧的正确方向不是"少报"，
是"只报真能属于某人的东西"：本脚本自己播种全部标记，`owner_of` 因此是全集，
不在全集里的数字不可能属于任何租户。
"""

from __future__ import annotations

import re

MARKER_PATTERN = re.compile(r"9\d{19}")


def find_leaks(text: str, owner_of: dict[str, str], current_tenant_id: str, name: str) -> list[str]:
    """返回响应体里出现的别的租户的标记。只认 `owner_of` 里的标记。"""
    leaks: list[str] = []
    for token in set(MARKER_PATTERN.findall(text)):
        owner = owner_of.get(token)
        if owner is not None and owner != current_tenant_id:
            leaks.append(f"{name}:marker_of_{owner}")
    return leaks
