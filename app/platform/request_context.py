"""请求关联 ID（NFR-005，`docs/06 §5.26`）。

一个请求要有唯一的 `request_id`，它必须同时出现在三处：**响应头**、**日志行**、
**审计事件**。缺任何一处，"把用户报的错和日志对上"这件事就做不成：
只进响应头，用户拿着 ID 来查日志查不到；只进日志，用户根本不知道自己的 ID 是什么。

## 实现要点

- **中间件生成或沿用**：请求头带 `X-Request-ID` 时沿用（便于前端与网关串联），
  否则生成一个。响应头一律回填，即使处理过程中抛异常。
- **上下文变量而非参数传递**：审计写入点在服务层，日志散在各处，让它们各自接收
  `request_id` 参数会改动几十个签名。用 `contextvars` 在中间件里设一次，
  下游按需读取——这也让后台线程天然拿不到请求上下文，避免把一个请求的 ID
  错误地贴到另一个请求的日志上。
- **格式校验**：只接受可打印 ASCII、长度 8–64。不校验的话，攻击者可以把
  换行或控制字符塞进 `X-Request-ID`，而它会被写进日志行与响应头（响应头里的
  换行更危险）。不合规一律换成本进程生成的新 ID，不报错——关联 ID 不值得
  让一个正常请求失败。
"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_LOGGER_FIELD = "request_id"

MIN_CHARS = 8
MAX_CHARS = 64
# 只允许可见 ASCII：排除空格与控制字符，控制字符进日志行会伪造出新行。
_ALLOWED = re.compile(r"^[\x21-\x7e]+$")

# 未设上下文时返回 None，而不是"上一个请求的 ID"——后者会让没有请求上下文的
# 日志行（例如 Worker 与派发器）错误地继承某个已结束请求的标识。
_current: ContextVar[str | None] = ContextVar("docpilot_request_id", default=None)


def generate() -> str:
    return uuid.uuid4().hex


def is_well_formed(value: str) -> bool:
    """长度与字符集校验；空、过长、含控制字符或空格一律不合格。"""
    if not MIN_CHARS <= len(value) <= MAX_CHARS:
        return False
    return _ALLOWED.match(value) is not None


def adopt_or_generate(supplied: str | None) -> str:
    """沿用调用方给的合规 ID，否则生成一个新的。不合规不报错，只替换。"""
    if supplied is not None and is_well_formed(supplied):
        return supplied
    return generate()


def current() -> str | None:
    """当前请求的 ID；不在请求上下文里（Worker、脚本、启动期）返回 None。"""
    return _current.get()


def set_current(value: str | None) -> object:
    """设置上下文，返回 `ContextVar` 令牌供调用方还原。"""
    return _current.set(value)


def reset(token: object) -> None:
    _current.reset(token)  # type: ignore[arg-type]


class RequestIdFilter(logging.Filter):
    """给每条日志记录补 `request_id` 字段。

    没有这个过滤器时，`%(request_id)s` 会直接把格式化整行打成异常；
    用 `-` 占位让"没有请求上下文"这件事在日志里是显式的，而不是空白。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current() or "-"
        return True


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个请求建立关联 ID 上下文，并在响应头回填。"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request, call_next):
        request_id = adopt_or_generate(request.headers.get(REQUEST_ID_HEADER))
        token = set_current(request_id)
        try:
            response = await call_next(request)
        finally:
            # 即使处理器抛异常也要清掉上下文，否则同一个 worker 处理下一个请求时
            # 可能读到上一个请求的 ID。
            reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """统一日志格式并挂上关联 ID 过滤器。

    三个后台进程入口（派发器、Worker、Webhook 投递）此前各写一份
    `basicConfig(format=...)`，格式里没有关联 ID，也没有过滤器。集中到一处后，
    "日志里带 request_id"这件事只有一个实现点；没有请求上下文时过滤器填 `-`。
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())