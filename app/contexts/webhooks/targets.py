"""出站目标校验（DOC-DD-021 §5）。

Webhook 是本项目第一次让服务端**主动**向外发起请求，默认按最小权限处理：
- 只允许 https；http 仅在本机测试开关打开且主机名是回环地址时放行。
- 带 userinfo 的 URL 直接拒：那等于把凭据写进一个会被日志与第三方看到的位置。
- 投递前解析主机名，命中回环/私有/链路本地/未指定地址即拒绝——除非显式打开
  `config.WEBHOOK_ALLOW_PRIVATE_TARGETS`（本机测试用）。

创建时只做语法与策略检查（DNS 可能瞬时故障，不能因此让人存不下端点）；
真正的 IP 判定在投递时做，解析失败按可重试的网络错误处理。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.platform import config
from app.contexts.webhooks.domain import MAX_URL_CHARS
from app.platform.errors import AccessError

_LOOPBACK_NAMES = {"localhost"}


def check_target(url: str) -> str:
    """校验并返回规范化后的 URL；不合规一律 422。"""
    candidate = (url or "").strip()
    if not candidate or len(candidate) > MAX_URL_CHARS:
        raise AccessError(422, f"回调地址长度需在 1-{MAX_URL_CHARS} 之间")
    parsed = urlparse(candidate)
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http":
        if not config.WEBHOOK_ALLOW_INSECURE_TARGETS:
            raise AccessError(422, "回调地址必须是 https")
        if parsed.hostname not in _LOOPBACK_NAMES and not _is_loopback_ip(parsed.hostname or ""):
            raise AccessError(422, "http 只允许指向本机回环地址（测试用途）")
    else:
        raise AccessError(422, "回调地址只支持 https")
    if parsed.username or parsed.password:
        raise AccessError(422, "回调地址不得包含凭据（user:pass@host）")
    if not parsed.netloc:
        raise AccessError(422, "回调地址缺少主机名")
    if parsed.fragment:
        raise AccessError(422, "回调地址不得包含片段")
    return parsed.geturl()


def _is_loopback_ip(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback()
    except ValueError:
        return False


def blocked_address(host: str) -> str | None:
    """解析主机名并判断是否落在禁止网段；返回拒绝原因，None 表示放行。"""
    if config.WEBHOOK_ALLOW_PRIVATE_TARGETS:
        return None
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None  # 交给投递当作可重试的连接错误，而不是在这里判定为"被禁"
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            return f"目标解析到本机或链路本地地址（{address}）"
        if address.is_private:
            return f"目标解析到内网地址（{address}）"
    return None
