"""文档对象键与本地存储路径边界校验。"""

from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath


def resolve_storage_path(storage_key: str, storage_root: Path) -> Path:
    """将相对对象键解析到存储根目录内，拒绝绝对路径、穿越和越界符号链接。"""
    if not storage_key or "\\" in storage_key:
        raise ValueError("无效的存储键")
    key = PurePosixPath(storage_key)
    if (
        key.is_absolute()
        or not key.parts
        or any(part in {".", ".."} for part in key.parts)
        or ":" in key.parts[0]
    ):
        raise ValueError("存储键必须是根目录内的相对路径")

    root = Path(storage_root).resolve()
    candidate = root.joinpath(*key.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("存储键解析后超出存储根目录") from exc
    return candidate
