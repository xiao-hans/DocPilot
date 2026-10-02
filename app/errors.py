"""跨上下文共用的领域错误。"""

from __future__ import annotations


class AccessError(Exception):
    """带 HTTP 状态码的业务拒绝；由 `app/main.py` 的异常处理器统一翻译。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
