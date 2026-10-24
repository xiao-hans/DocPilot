"""导出文件落盘：并发下载同一任务时不允许读到半新半旧的压缩包。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path


def build_atomically(build: Callable[[Path], object], destination: Path) -> Path:
    """先写同目录临时文件再原子改名，避免两个请求交错写同一个路径。"""
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"{destination.name}.", suffix=".part", dir=str(destination.parent)
    )
    os.close(descriptor)
    staging = Path(temporary)
    try:
        build(staging)
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)
    return destination
