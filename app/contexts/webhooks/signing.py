"""签名密钥的派生与验签（DOC-DD-021 §4）。

密钥不落库：`secret = HMAC_SHA256(master, f"{endpoint_id}|{secret_version}")`。
数据库单独被读走拿不到签名能力，轮换也只是推进版本号。

签名覆盖 `"<timestamp>.<body>"`，body 是**实际发出的那串字节**——序列化一次、发送与签名
共用同一份，否则任何 re-serialize 差异都会让接收方验不过。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from app.contexts.webhooks.domain import SIGNATURE_PREFIX

SIGNATURE_HEADER = "X-DocPilot-Signature"
TIMESTAMP_HEADER = "X-DocPilot-Timestamp"
DELIVERY_HEADER = "X-DocPilot-Delivery"
EVENT_HEADER = "X-DocPilot-Event"
SECRET_VERSION_HEADER = "X-DocPilot-Secret-Version"
# 稳定字节形状：键排序 + 紧凑分隔符，两方言、两进程都得出一样的结果。
JSON_DUMP_KWARGS: dict[str, Any] = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": False}


def derive_secret(master_key: str, endpoint_id: str, secret_version: int) -> str:
    if not master_key:
        raise ValueError("WEBHOOK_MASTER_KEY 未配置：出站签名不可用")
    digest = hmac.new(
        master_key.encode("utf-8"), f"{endpoint_id}|{secret_version}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"whsec_{digest}"


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, **JSON_DUMP_KWARGS).encode("utf-8")


def sign(secret: str, timestamp: int, body: bytes) -> str:
    signed = f"{timestamp}.".encode("ascii") + body
    return SIGNATURE_PREFIX + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: int, body: bytes, header_value: str) -> bool:
    """接收方口径的参考实现，也是用例里的正向断言工具。"""
    if not header_value.startswith(SIGNATURE_PREFIX):
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), header_value)


def fingerprint(secret: str) -> str:
    """列表接口只回末 6 位：够人核对是哪一把，不足以拿去签名。"""
    return f"…{secret[-6:]}" if len(secret) > 6 else "…"
